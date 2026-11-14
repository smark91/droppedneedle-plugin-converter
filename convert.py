#!/usr/bin/env python3
"""Auto-convert downloaded audio to Opus for DroppedNeedle's import folder.

Triggered via slskd integration script or run manually from the command line.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

LOSSLESS_EXTENSIONS = {".flac", ".wav", ".alac", ".aif", ".aiff"}
LOSSY_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".wma"}
AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | LOSSY_EXTENSIONS
SIDECAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".cue", ".log"}


@dataclass(frozen=True)
class ConversionConfig:
    source_dir: Path
    import_dir: Path
    bitrate: str = "160k"
    convert_lossy: bool = False
    keep_source: bool = True


@dataclass
class Plan:
    audio_to_convert: List[Path] = field(default_factory=list)
    audio_to_copy: List[Path] = field(default_factory=list)
    sidecars_to_copy: List[Path] = field(default_factory=list)


def parse_slskd_payload(raw_json: str) -> Optional[Path]:
    """Parse slskd's SLSKD_SCRIPT_DATA environment variable into a source directory."""
    if not raw_json or not raw_json.strip():
        return None
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as err:
        logging.error("Failed to parse SLSKD_SCRIPT_DATA as JSON: %s", err)
        return None

    # slskd uses localDirectoryName or directory depending on version and event
    candidates = [
        data.get("localDirectoryName"),
        data.get("directory"),
        data.get("localPath"),
        data.get("path"),
    ]
    for candidate in candidates:
        if candidate and isinstance(candidate, str):
            path = Path(candidate)
            if path.exists():
                return path

    logging.warning("No existing path found in SLSKD_SCRIPT_DATA keys: %s", data)
    return None


def plan_directory(source_dir: Path, convert_lossy: bool) -> Plan:
    """Analyze files in source directory and build conversion plan."""
    plan = Plan()
    for root, _, files in os.walk(source_dir):
        root_path = Path(root)
        for name in files:
            file_path = root_path / name
            suffix = file_path.suffix.lower()

            if suffix in LOSSLESS_EXTENSIONS:
                plan.audio_to_convert.append(file_path)
            elif suffix in LOSSY_EXTENSIONS:
                if convert_lossy:
                    plan.audio_to_convert.append(file_path)
                else:
                    plan.audio_to_copy.append(file_path)
            elif suffix in SIDECAR_EXTENSIONS:
                plan.sidecars_to_copy.append(file_path)

    return plan


def transcode_to_opus(source_file: Path, dest_file: Path, bitrate: str) -> bool:
    """Transcode one audio file to Opus using FFmpeg, preserving metadata."""
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest_file.with_suffix(".tmp.opus")

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source_file),
        "-c:a",
        "libopus",
        "-b:a",
        bitrate,
        "-vbr",
        "on",
        "-map_metadata",
        "0",
        str(temp_dest),
    ]

    try:
        subprocess.run(cmd, check=True)
        temp_dest.replace(dest_file)
        return True
    except subprocess.CalledProcessError as err:
        logging.error("FFmpeg transcode failed for %s: %s", source_file, err)
        if temp_dest.exists():
            temp_dest.unlink()
        return False


def execute_plan(config: ConversionConfig, plan: Plan) -> bool:
    """Convert files into a staging folder and atomically move to import folder."""
    if not plan.audio_to_convert and not plan.audio_to_copy:
        logging.info("No audio files found in %s. Skipping.", config.source_dir)
        return False

    folder_name = config.source_dir.name
    final_dest = config.import_dir / folder_name
    staging_dest = config.import_dir / f".staging_{folder_name}"

    if final_dest.exists():
        logging.info("Destination %s already exists. Skipping to avoid collision.", final_dest)
        return False

    if staging_dest.exists():
        shutil.rmtree(staging_dest)
    staging_dest.mkdir(parents=True, exist_ok=True)

    success = True

    # 1. Transcode lossless/selected audio to Opus
    for src_audio in plan.audio_to_convert:
        rel_path = src_audio.relative_to(config.source_dir)
        target_audio = (staging_dest / rel_path).with_suffix(".opus")
        if not transcode_to_opus(src_audio, target_audio, config.bitrate):
            success = False
            break

    # 2. Copy remaining audio as-is
    if success:
        for src_audio in plan.audio_to_copy:
            rel_path = src_audio.relative_to(config.source_dir)
            target_audio = staging_dest / rel_path
            target_audio.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_audio, target_audio)

    # 3. Copy sidecars (artwork, cue, log)
    if success:
        for src_sidecar in plan.sidecars_to_copy:
            rel_path = src_sidecar.relative_to(config.source_dir)
            target_sidecar = staging_dest / rel_path
            target_sidecar.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_sidecar, target_sidecar)

    if not success:
        logging.error("Conversion failed. Cleaning up staging folder %s", staging_dest)
        shutil.rmtree(staging_dest, ignore_errors=True)
        return False

    # 4. Atomic handoff to DroppedNeedle's import folder
    try:
        staging_dest.replace(final_dest)
        logging.info("Successfully delivered %s to %s", folder_name, final_dest)
    except OSError as err:
        logging.error("Failed to move %s to %s: %s", staging_dest, final_dest, err)
        shutil.rmtree(staging_dest, ignore_errors=True)
        return False

    # 5. Optional cleanup of source folder
    if not config.keep_source:
        logging.info("Removing source directory %s", config.source_dir)
        shutil.rmtree(config.source_dir, ignore_errors=True)

    return True


def run(config: ConversionConfig) -> int:
    """Run conversion workflow for one source directory."""
    if not config.source_dir.is_dir():
        logging.error("Source directory %s is not a directory or does not exist.", config.source_dir)
        return 1

    config.import_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_directory(config.source_dir, config.convert_lossy)
    ok = execute_plan(config, plan)
    return 0 if ok else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Auto-convert slskd downloads to Opus for DroppedNeedle")
    parser.add_argument("source", nargs="?", help="Source album directory (defaults to SLSKD_SCRIPT_DATA)")
    parser.add_argument(
        "--import-dir",
        default=os.environ.get("DROPPEDNEEDLE_IMPORT_DIR", "/data/import"),
        help="DroppedNeedle import folder path (default: /data/import)",
    )
    parser.add_argument(
        "--bitrate",
        default=os.environ.get("OPUS_BITRATE", "160k"),
        help="Target Opus bitrate (default: 160k)",
    )
    parser.add_argument(
        "--convert-lossy",
        action="store_true",
        default=os.environ.get("CONVERT_LOSSY", "").lower() in ("true", "1", "yes"),
        help="Also convert MP3/AAC to Opus (warning: lossy to lossy generation loss)",
    )
    parser.add_argument(
        "--delete-source",
        action="store_true",
        default=os.environ.get("DELETE_SOURCE", "").lower() in ("true", "1", "yes"),
        help="Delete source folder in slskd downloads after successful conversion",
    )

    args = parser.parse_args()

    source_path = None
    if args.source:
        source_path = Path(args.source)
    else:
        env_payload = os.environ.get("SLSKD_SCRIPT_DATA", "")
        source_path = parse_slskd_payload(env_payload)

    if not source_path:
        logging.error("No source directory specified and no SLSKD_SCRIPT_DATA environment variable found.")
        sys.exit(1)

    config = ConversionConfig(
        source_dir=source_path,
        import_dir=Path(args.import_dir),
        bitrate=args.bitrate,
        convert_lossy=args.convert_lossy,
        keep_source=not args.delete_source,
    )

    sys.exit(run(config))


if __name__ == "__main__":
    main()
