#!/usr/bin/env python3
import asyncio
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from plugin import (
    AutoOpusConverter,
    TrackRecord,
    calculate_path_hash,
    find_tracks_in_db,
    transcode_file,
    update_track_in_db,
)


class TestPlugin(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_plugin_"))
        self.cache_dir = self.test_dir / "cache"
        self.cache_dir.mkdir(parents=True)
        self.db_path = self.cache_dir / "library.db"
        self.music_dir = self.test_dir / "music"
        self.music_dir.mkdir(parents=True)

        self._init_mock_db()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_mock_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE local_tracks (
                id TEXT PRIMARY KEY,
                local_album_id TEXT,
                root_id TEXT,
                file_path TEXT,
                relative_path TEXT,
                path_hash TEXT,
                file_size_bytes INTEGER,
                file_mtime_ns INTEGER,
                stat_revision TEXT,
                stat_revision_kind TEXT,
                file_format TEXT,
                download_task_id TEXT,
                row_revision INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE local_album_external_identities (
                local_album_id TEXT PRIMARY KEY,
                release_group_mbid TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _create_synthetic_wav(self, path: Path, duration_sec: float = 0.5):
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_sec}",
            str(path),
        ]
        subprocess.run(cmd, check=True)

    def test_calculate_path_hash(self):
        path = "Artist/Album/01. Track.opus"
        expected = hashlib.sha256(path.encode("utf-8")).hexdigest()
        self.assertEqual(calculate_path_hash(path), expected)

    def test_transcode_file(self):
        src = self.music_dir / "test.wav"
        dest = self.music_dir / "test.opus"
        self._create_synthetic_wav(src)

        ok = transcode_file(src, dest, "128k")
        self.assertTrue(ok)
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 0)

    def test_db_operations(self):
        wav_file = self.music_dir / "01. Song.wav"
        self._create_synthetic_wav(wav_file)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks VALUES "
            "('trk_1', 'alb_1', 'r1', ?, 'Artist/Album/01. Song.wav', 'hash1', 100, 1000, 'rev1', 'exact', 'wav', 'task_123', 1)",
            (str(wav_file),),
        )
        cur.execute(
            "INSERT INTO local_album_external_identities VALUES ('alb_1', 'mbid_abc')"
        )
        conn.commit()
        conn.close()

        # Find by task_id
        tracks = find_tracks_in_db(self.db_path, "task_123", None)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].track_id, "trk_1")

        # Find by mbid
        tracks_by_mbid = find_tracks_in_db(self.db_path, None, "mbid_abc")
        self.assertEqual(len(tracks_by_mbid), 1)
        self.assertEqual(tracks_by_mbid[0].track_id, "trk_1")

        # Update track in DB
        opus_file = self.music_dir / "01. Song.opus"
        self._create_synthetic_wav(opus_file)
        new_rel = "Artist/Album/01. Song.opus"
        updated = update_track_in_db(self.db_path, tracks[0], opus_file, new_rel)
        self.assertTrue(updated)

        # Verify DB changes
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT file_path, relative_path, file_format, row_revision FROM local_tracks WHERE id = 'trk_1'")
        row = cur.fetchone()
        conn.close()

        self.assertEqual(row[0], str(opus_file))
        self.assertEqual(row[1], new_rel)
        self.assertEqual(row[2], "opus")
        self.assertEqual(row[3], 2)

    def test_plugin_event_handling_and_worker(self):
        wav_file = self.music_dir / "01. Song.flac"
        self._create_synthetic_wav(wav_file)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks VALUES "
            "('trk_1', 'alb_1', 'r1', ?, 'Artist/Album/01. Song.flac', 'hash1', 100, 1000, 'rev1', 'exact', 'flac', 'task_999', 1)",
            (str(wav_file),),
        )
        conn.commit()
        conn.close()

        ctx = MagicMock()
        ctx.logger = logging.getLogger("test_plugin")
        ctx.settings = {
            "cache_path": str(self.cache_dir),
            "bitrate": "128k",
            "convert_lossy": "false",
        }

        plugin = AutoOpusConverter(ctx)

        event = MagicMock()
        event.kind = "download_completed"
        event.payload = {
            "task_id": "task_999",
            "release_group_mbid": "mbid_xyz",
            "outcome": "completed",
        }

        asyncio.run(plugin.on_event(event))

        # Wait for worker thread to process task
        plugin.queue.join()

        opus_file = self.music_dir / "01. Song.opus"
        self.assertTrue(opus_file.exists())
        self.assertFalse(wav_file.exists())

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT file_format FROM local_tracks WHERE id = 'trk_1'")
        fmt = cur.fetchone()[0]
        conn.close()
        self.assertEqual(fmt, "opus")


if __name__ == "__main__":
    unittest.main()
