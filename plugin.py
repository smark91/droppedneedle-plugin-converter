"""DroppedNeedle v3 plugin to auto-convert downloaded FLAC tracks to Opus."""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import List, Optional, Set, Tuple

LOSSLESS_EXTS = {".flac", ".wav", ".alac", ".aif", ".aiff"}
LOSSY_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".wma"}


@dataclass
class TrackRecord:
    track_id: str
    file_path: Path
    relative_path: str
    root_id: str
    row_revision: int


def calculate_path_hash(relative_path: str) -> str:
    """Compute sha256 hex digest of the relative path."""
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def transcode_file(source: Path, target: Path, bitrate: str) -> bool:
    """Transcode source audio to Opus using system FFmpeg with metadata preservation."""
    temp_target = target.with_suffix(".tmp.opus")
    target.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-c:a",
        "libopus",
        "-b:a",
        bitrate,
        "-vbr",
        "on",
        "-map_metadata",
        "0",
        str(temp_target),
    ]

    try:
        subprocess.run(cmd, check=True)
        temp_target.replace(target)
        return True
    except subprocess.CalledProcessError as err:
        logging.error("FFmpeg error transcoding %s: %s", source, err)
        if temp_target.exists():
            temp_target.unlink()
        return False


def find_tracks_in_db(
    db_path: Path, task_id: Optional[str], release_group_mbid: Optional[str]
) -> List[TrackRecord]:
    """Find track paths and identifiers in DroppedNeedle's library.db."""
    if not db_path.exists():
        return []

    tracks: List[TrackRecord] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # 1. Match by download_task_id
        if task_id and task_id != "__sweep__":
            cursor.execute(
                "SELECT id, file_path, relative_path, root_id, row_revision "
                "FROM local_tracks WHERE download_task_id = ?",
                (task_id,),
            )
            for row in cursor.fetchall():
                tracks.append(
                    TrackRecord(
                        track_id=row[0],
                        file_path=Path(row[1]),
                        relative_path=row[2],
                        root_id=row[3],
                        row_revision=row[4],
                    )
                )

        # 2. Match by release_group_mbid
        if not tracks and release_group_mbid:
            cursor.execute(
                "SELECT t.id, t.file_path, t.relative_path, t.root_id, t.row_revision "
                "FROM local_tracks t "
                "JOIN local_album_external_identities i ON t.local_album_id = i.local_album_id "
                "WHERE lower(i.release_group_mbid) = lower(?)",
                (release_group_mbid,),
            )
            for row in cursor.fetchall():
                tracks.append(
                    TrackRecord(
                        track_id=row[0],
                        file_path=Path(row[1]),
                        relative_path=row[2],
                        root_id=row[3],
                        row_revision=row[4],
                    )
                )

        conn.close()
    except Exception as err:
        logging.warning("Failed querying library.db at %s: %s", db_path, err)

    return tracks


def find_unconverted_tracks_in_db(db_path: Path, convert_lossy: bool) -> List[TrackRecord]:
    """Find any remaining unconverted lossless (or lossy) tracks in library.db."""
    if not db_path.exists():
        return []

    tracks: List[TrackRecord] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        formats = ["flac", "wav", "alac", "aiff"]
        if convert_lossy:
            formats.extend(["mp3", "m4a", "aac", "ogg", "wma"])

        placeholders = ",".join("?" for _ in formats)
        query = (
            f"SELECT id, file_path, relative_path, root_id, row_revision "
            f"FROM local_tracks WHERE lower(file_format) IN ({placeholders}) "
            f"AND availability = 'indexed'"
        )
        cursor.execute(query, formats)
        for row in cursor.fetchall():
            tracks.append(
                TrackRecord(
                    track_id=row[0],
                    file_path=Path(row[1]),
                    relative_path=row[2],
                    root_id=row[3],
                    row_revision=row[4],
                )
            )
        conn.close()
    except Exception as err:
        logging.warning("Failed querying unconverted tracks from %s: %s", db_path, err)

    return tracks


def update_track_in_db(
    db_path: Path,
    track: TrackRecord,
    new_file_path: Path,
    new_relative_path: str,
) -> bool:
    """Update track path, hash, and stat revision in library.db to prevent catalog churn."""
    try:
        stat_result = new_file_path.stat()
        file_size = stat_result.st_size
        mtime_ns = getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9))
        stat_rev = f"{file_size}:{mtime_ns}"
        path_hash = calculate_path_hash(new_relative_path)

        conn = sqlite3.connect(db_path, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE local_tracks SET "
            "file_path = ?, relative_path = ?, path_hash = ?, "
            "file_size_bytes = ?, file_mtime_ns = ?, stat_revision = ?, "
            "stat_revision_kind = 'exact', file_format = 'opus', "
            "row_revision = row_revision + 1 "
            "WHERE id = ?",
            (
                str(new_file_path),
                new_relative_path,
                path_hash,
                file_size,
                mtime_ns,
                stat_rev,
                track.track_id,
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as err:
        logging.error("Failed to update track %s in library.db: %s", track.track_id, err)
        return False


class AutoOpusConverter:
    """DroppedNeedle plugin listening to download and request events and transcoding to Opus."""

    def __init__(self, context):
        self.ctx = context
        self.queue: Queue = Queue()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    async def on_event(self, event) -> None:
        """Handle incoming server events within 5 seconds."""
        kind = getattr(event, "kind", "")
        # Accept download_completed, request_fulfilled, and import_finished to safeguard against DroppedNeedle's dropped event limit
        if kind not in ("download_completed", "request_fulfilled", "import_finished"):
            return

        payload = getattr(event, "payload", {})
        task_id = getattr(payload, "task_id", "") or (payload.get("task_id") if isinstance(payload, dict) else "")
        rg_mbid = getattr(payload, "release_group_mbid", "") or (
            payload.get("release_group_mbid") if isinstance(payload, dict) else ""
        )

        # For download_completed, ensure outcome is completed or partial
        if kind == "download_completed":
            outcome = getattr(payload, "outcome", "") or (payload.get("outcome") if isinstance(payload, dict) else "")
            if outcome not in ("completed", "partial"):
                return

        if task_id or rg_mbid:
            self.ctx.logger.info("Queued auto-conversion on event %s (task: %s, mbid: %s)", kind, task_id, rg_mbid)
            self.queue.put((task_id, rg_mbid))

    async def on_tick(self) -> None:
        """Periodic safety net to catch any unconverted tracks whose events were dropped."""
        self.queue.put(("__sweep__", ""))

    def _worker_loop(self) -> None:
        """Process queued conversion tasks sequentially in background."""
        while True:
            try:
                task_id, rg_mbid = self.queue.get(timeout=2.0)
            except Empty:
                continue

            try:
                if task_id == "__sweep__":
                    self._process_sweep()
                else:
                    self._process_download(task_id, rg_mbid)
            except Exception as err:
                self.ctx.logger.error("Error processing task %s / %s: %s", task_id, rg_mbid, err)
            finally:
                self.queue.task_done()

    def _process_sweep(self) -> None:
        """Sweep library.db and transcode any unconverted files."""
        cache_path = Path(self.ctx.settings.get("cache_path") or "/app/cache")
        db_path = cache_path / "library.db"
        bitrate = self.ctx.settings.get("bitrate") or "160k"
        convert_lossy = str(self.ctx.settings.get("convert_lossy", "")).lower() in ("true", "1", "yes")

        tracks = find_unconverted_tracks_in_db(db_path, convert_lossy)
        if tracks:
            self.ctx.logger.info("Periodic sweep found %d unconverted track(s)", len(tracks))
            self._convert_tracks(tracks, db_path, bitrate, convert_lossy)

    def _process_download(self, task_id: str, rg_mbid: str) -> None:
        cache_path = Path(self.ctx.settings.get("cache_path") or "/app/cache")
        db_path = cache_path / "library.db"
        bitrate = self.ctx.settings.get("bitrate") or "160k"
        convert_lossy = str(self.ctx.settings.get("convert_lossy", "")).lower() in ("true", "1", "yes")

        tracks = find_tracks_in_db(db_path, task_id, rg_mbid)
        if not tracks:
            # Short sleep and retry once in case the database transaction is settling
            time.sleep(1.0)
            tracks = find_tracks_in_db(db_path, task_id, rg_mbid)

        if not tracks:
            self.ctx.logger.info("No matching tracks in database for task %s / mbid %s", task_id, rg_mbid)
            return

        self._convert_tracks(tracks, db_path, bitrate, convert_lossy)

    def _convert_tracks(
        self, tracks: List[TrackRecord], db_path: Path, bitrate: str, convert_lossy: bool
    ) -> None:
        for track in tracks:
            src = track.file_path
            suffix = src.suffix.lower()

            should_convert = (suffix in LOSSLESS_EXTS) or (convert_lossy and suffix in LOSSY_EXTS)
            if not should_convert or not src.exists():
                continue

            dest = src.with_suffix(".opus")
            rel_dest = str(Path(track.relative_path).with_suffix(".opus"))

            self.ctx.logger.info("Transcoding %s -> %s", src.name, dest.name)
            if transcode_file(src, dest, bitrate):
                db_updated = False
                if db_path.exists():
                    db_updated = update_track_in_db(db_path, track, dest, rel_dest)

                try:
                    src.unlink()
                except OSError as err:
                    self.ctx.logger.warning("Could not delete original file %s: %s", src, err)

                self.ctx.logger.info(
                    "Transcoded %s successfully (db updated: %s)", dest.name, db_updated
                )
