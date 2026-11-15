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
    find_unconverted_tracks_in_db,
    transcode_file,
    update_track_in_db,
    __version__,
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
                row_revision INTEGER,
                availability TEXT DEFAULT 'indexed',
                imported_at INTEGER DEFAULT 0
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

    def _create_flac_with_art(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        wav_path = self.test_dir / "temp_audio.wav"
        img_path = self.test_dir / "temp_art.png"
        self._create_synthetic_wav(wav_path)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=100x100:d=1", "-vframes", "1", str(img_path)],
            check=True,
        )
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
            "-i", str(wav_path), "-i", str(img_path),
            "-map", "0:a", "-map", "1:v",
            "-c:a", "flac", "-c:v", "copy",
            "-disposition:v", "attached_pic",
            str(path),
        ]
        subprocess.run(cmd, check=True)

    def _create_flac_multichannel(self, path: Path):
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
            "sine=frequency=440:duration=0.5",
            "-filter_complex",
            "[0:a]channelmap=channel_layout=5.1(side):map=0|0|0|0|0|0[aout]",
            "-map",
            "[aout]",
            str(path),
        ]
        subprocess.run(cmd, check=True)

    def test_version_consistency(self):
        toml_path = Path(__file__).parent / "plugin.toml"
        self.assertTrue(toml_path.exists())
        version_line = [
            line.strip()
            for line in toml_path.read_text().splitlines()
            if line.strip().startswith("version =")
        ]
        self.assertTrue(len(version_line) > 0)
        toml_version = version_line[0].split("=")[1].strip().strip('"').strip("'")
        self.assertEqual(__version__, toml_version)
        self.assertEqual(__version__, "1.2.0")

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

    def test_transcode_flac_with_attached_picture(self):
        src = self.music_dir / "art.flac"
        dest = self.music_dir / "art.opus"
        self._create_flac_with_art(src)

        ok = transcode_file(src, dest, "128k")
        self.assertTrue(ok)
        self.assertTrue(dest.exists())
        self.assertGreater(dest.stat().st_size, 0)

        # Folder cover image was written
        cover_file = dest.parent / "cover.png"
        self.assertTrue(cover_file.exists())
        self.assertGreater(cover_file.stat().st_size, 0)

        # Artwork was also embedded as RFC 7845 METADATA_BLOCK_PICTURE
        probe = subprocess.run(["ffmpeg", "-i", str(dest)], capture_output=True, text=True)
        self.assertIn("attached pic", probe.stderr)

    def test_transcode_multichannel_5_1_side(self):
        src = self.music_dir / "surround.flac"
        dest = self.music_dir / "surround.opus"
        self._create_flac_multichannel(src)

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
            "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, stat_revision_kind, file_format, download_task_id, row_revision, availability) VALUES "
            "('trk_1', 'alb_1', 'r1', ?, 'Artist/Album/01. Song.wav', 'hash1', 100, 1000, 'rev1', 'exact', 'wav', 'task_123', 1, 'indexed')",
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

    def test_request_fulfilled_event_triggers_conversion(self):
        wav_file = self.music_dir / "01. Song.flac"
        self._create_synthetic_wav(wav_file)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, stat_revision_kind, file_format, download_task_id, row_revision, availability) VALUES "
            "('trk_1', 'alb_1', 'r1', ?, 'Artist/Album/01. Song.flac', 'hash1', 100, 1000, 'rev1', 'exact', 'flac', '', 1, 'indexed')",
            (str(wav_file),),
        )
        cur.execute(
            "INSERT INTO local_album_external_identities VALUES ('alb_1', 'mbid_request_test')"
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

        # Send request_fulfilled (which arrives even when download_completed is dropped)
        event = MagicMock()
        event.kind = "request_fulfilled"
        event.payload = {
            "request_id": "req_1",
            "release_group_mbid": "mbid_request_test",
            "status": "fulfilled",
        }

        asyncio.run(plugin.on_event(event))
        plugin.queue.join()

        opus_file = self.music_dir / "01. Song.opus"
        self.assertTrue(opus_file.exists())
        self.assertFalse(wav_file.exists())

    def test_periodic_sweep(self):
        wav_file = self.music_dir / "02. SweepSong.flac"
        self._create_synthetic_wav(wav_file)

        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, stat_revision_kind, file_format, download_task_id, row_revision, availability, imported_at) VALUES "
            "('trk_2', 'alb_2', 'r1', ?, 'Artist/Album/02. SweepSong.flac', 'hash2', 100, 1000, 'rev2', 'exact', 'flac', '', 1, 'indexed', ?)",
            (str(wav_file), now_ms + 1000),
        )
        conn.commit()
        conn.close()

        ctx = MagicMock()
        ctx.logger = logging.getLogger("test_plugin")
        ctx.settings = {
            "cache_path": str(self.cache_dir),
            "bitrate": "128k",
            "convert_lossy": "false",
            "convert_existing": "false",
        }

        plugin = AutoOpusConverter(ctx)

        # Trigger on_tick - newly imported track is caught
        asyncio.run(plugin.on_tick())
        plugin.queue.join()

        opus_file = self.music_dir / "02. SweepSong.opus"
        self.assertTrue(opus_file.exists())
        self.assertFalse(wav_file.exists())

    def test_periodic_sweep_ignores_old_files_by_default(self):
        wav_file = self.music_dir / "03. OldSong.flac"
        self._create_synthetic_wav(wav_file)

        # imported_at is well before plugin starts
        old_imported_ms = 1000
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, stat_revision_kind, file_format, download_task_id, row_revision, availability, imported_at) VALUES "
            "('trk_old', 'alb_old', 'r1', ?, 'Artist/Album/03. OldSong.flac', 'hash_old', 100, 1000, 'rev_old', 'exact', 'flac', '', 1, 'indexed', ?)",
            (str(wav_file), old_imported_ms),
        )
        conn.commit()
        conn.close()

        ctx = MagicMock()
        ctx.logger = logging.getLogger("test_plugin")
        ctx.settings = {
            "cache_path": str(self.cache_dir),
            "bitrate": "128k",
            "convert_lossy": "false",
            "convert_existing": "false",
        }

        plugin = AutoOpusConverter(ctx)

        asyncio.run(plugin.on_tick())
        plugin.queue.join()

        opus_file = self.music_dir / "03. OldSong.opus"
        # Old file was NOT converted
        self.assertFalse(opus_file.exists())
        self.assertTrue(wav_file.exists())

    def test_periodic_sweep_converts_old_files_when_enabled(self):
        wav_file = self.music_dir / "04. OldConvertedSong.flac"
        self._create_synthetic_wav(wav_file)

        old_imported_ms = 1000
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, stat_revision_kind, file_format, download_task_id, row_revision, availability, imported_at) VALUES "
            "('trk_old_conv', 'alb_old', 'r1', ?, 'Artist/Album/04. OldConvertedSong.flac', 'hash_old2', 100, 1000, 'rev_old2', 'exact', 'flac', '', 1, 'indexed', ?)",
            (str(wav_file), old_imported_ms),
        )
        conn.commit()
        conn.close()

        ctx = MagicMock()
        ctx.logger = logging.getLogger("test_plugin")
        ctx.settings = {
            "cache_path": str(self.cache_dir),
            "bitrate": "128k",
            "convert_lossy": "false",
            "convert_existing": "true",
        }

        plugin = AutoOpusConverter(ctx)

        asyncio.run(plugin.on_tick())
        plugin.queue.join()

        opus_file = self.music_dir / "04. OldConvertedSong.opus"
        # Old file was converted because convert_existing=true
        self.assertTrue(opus_file.exists())
        self.assertFalse(wav_file.exists())

    def test_delete_source_false_keeps_original(self):
        wav_file = self.music_dir / "05. RetainSource.flac"
        self._create_synthetic_wav(wav_file)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO local_tracks (id, local_album_id, root_id, file_path, relative_path, path_hash, file_size_bytes, file_mtime_ns, stat_revision, stat_revision_kind, file_format, download_task_id, row_revision, availability) VALUES "
            "('trk_retain', 'alb_retain', 'r1', ?, 'Artist/Album/05. RetainSource.flac', 'hash_ret', 100, 1000, 'rev_ret', 'exact', 'flac', 'task_retain', 1, 'indexed')",
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
            "delete_source": "false",
        }

        plugin = AutoOpusConverter(ctx)

        event = MagicMock()
        event.kind = "download_completed"
        event.payload = {"task_id": "task_retain", "outcome": "completed"}

        asyncio.run(plugin.on_event(event))
        plugin.queue.join()

        opus_file = self.music_dir / "05. RetainSource.opus"
        self.assertTrue(opus_file.exists())
        # Original file was KEPT because delete_source=false
        self.assertTrue(wav_file.exists())

    def test_enable_sweep_disabled(self):
        ctx = MagicMock()
        ctx.logger = logging.getLogger("test_plugin")
        ctx.settings = {
            "enable_sweep": "false",
        }

        plugin = AutoOpusConverter(ctx)
        self.assertEqual(plugin.queue.qsize(), 0)

        asyncio.run(plugin.on_tick())
        # Nothing added to queue because sweep is disabled
        self.assertEqual(plugin.queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
