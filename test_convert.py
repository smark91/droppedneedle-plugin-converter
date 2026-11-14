#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from convert import (
    ConversionConfig,
    execute_plan,
    parse_slskd_payload,
    plan_directory,
    transcode_to_opus,
)


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_convert_"))
        self.source_dir = self.test_dir / "downloads" / "Artist - Album"
        self.source_dir.mkdir(parents=True)
        self.import_dir = self.test_dir / "import"
        self.import_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_synthetic_wav(self, path: Path, duration_sec: float = 0.5):
        """Generate a short synthetic WAV using FFmpeg."""
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

    def test_parse_slskd_payload(self):
        payload_local = json.dumps({"localDirectoryName": str(self.source_dir)})
        self.assertEqual(parse_slskd_payload(payload_local), self.source_dir)

        payload_dir = json.dumps({"directory": str(self.source_dir)})
        self.assertEqual(parse_slskd_payload(payload_dir), self.source_dir)

        self.assertIsNone(parse_slskd_payload("invalid json"))
        self.assertIsNone(parse_slskd_payload(json.dumps({"nonexistent": "/fake/path"})))

    def test_plan_directory_flac_and_sidecars(self):
        flac_file = self.source_dir / "01. Song.flac"
        mp3_file = self.source_dir / "02. Song.mp3"
        cover_file = self.source_dir / "cover.jpg"
        flac_file.touch()
        mp3_file.touch()
        cover_file.touch()

        # By default, do not convert lossy (keep mp3 as-is)
        plan = plan_directory(self.source_dir, convert_lossy=False)
        self.assertEqual(plan.audio_to_convert, [flac_file])
        self.assertEqual(plan.audio_to_copy, [mp3_file])
        self.assertEqual(plan.sidecars_to_copy, [cover_file])

        # When convert_lossy is True
        plan_lossy = plan_directory(self.source_dir, convert_lossy=True)
        self.assertIn(flac_file, plan_lossy.audio_to_convert)
        self.assertIn(mp3_file, plan_lossy.audio_to_convert)
        self.assertEqual(plan_lossy.audio_to_copy, [])
        self.assertEqual(plan_lossy.sidecars_to_copy, [cover_file])

    def test_transcode_to_opus(self):
        src_wav = self.source_dir / "test.wav"
        self._create_synthetic_wav(src_wav)
        dest_opus = self.test_dir / "out" / "test.opus"

        success = transcode_to_opus(src_wav, dest_opus, bitrate="128k")
        self.assertTrue(success)
        self.assertTrue(dest_opus.exists())
        self.assertGreater(dest_opus.stat().st_size, 0)

    def test_execute_plan_end_to_end_and_idempotency(self):
        # 1. Prepare files
        track1 = self.source_dir / "01. Track.wav"
        cover = self.source_dir / "folder.jpg"
        self._create_synthetic_wav(track1)
        cover.write_text("fake cover data")

        config = ConversionConfig(
            source_dir=self.source_dir,
            import_dir=self.import_dir,
            bitrate="128k",
            convert_lossy=False,
            keep_source=True,
        )

        plan = plan_directory(self.source_dir, convert_lossy=False)
        success = execute_plan(config, plan)
        self.assertTrue(success)

        final_album = self.import_dir / "Artist - Album"
        self.assertTrue(final_album.exists())
        self.assertTrue((final_album / "01. Track.opus").exists())
        self.assertTrue((final_album / "folder.jpg").exists())

        # 2. Test idempotency: Running again should detect destination exists and skip cleanly
        second_run = execute_plan(config, plan)
        self.assertFalse(second_run)


if __name__ == "__main__":
    unittest.main()
