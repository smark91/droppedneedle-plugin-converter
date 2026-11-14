# DroppedNeedle Opus Conversion Pipeline

An automated post-download transcode pipeline for Soulseek (`slskd`) downloads into DroppedNeedle's native Import Folder.

## Why this exists

Soulseek distributions primarily offer FLAC or MP3. DroppedNeedle's internal pipeline lands downloaded files directly into the database-backed music library. If files are replaced or transcoded after landing, DroppedNeedle's database consistency checks treat the original tracks as missing and the new files as unregistered orphans, causing database churn and broken play history.

This pipeline intercepts completed downloads at the source (in `slskd`), transcodes audio to Opus using FFmpeg while preserving metadata tags and album sidecars (covers, cue sheets), and atomically deposits the album into DroppedNeedle's monitored Import Folder (`/data/import`).

DroppedNeedle's native `import_folder` engine then matches, tags, and files the Opus album cleanly on its first and only landing.

## Features

- **Lossless to Opus transcoding**: Converts FLAC, WAV, and ALAC to Opus via FFmpeg with `-map_metadata 0`.
- **Lossy preservation**: Keeps existing MP3 and AAC files as-is by default to prevent generational loss.
- **Sidecar copying**: Retains cover artwork (`.jpg`, `.png`), `.cue`, and `.log` files.
- **Atomic delivery**: Builds albums inside a hidden `.staging_<album>` directory and performs an atomic rename once finished. DroppedNeedle never encounters partially written files.
- **Zero external Python dependencies**: Standard library only.

## Setup

### 1. In DroppedNeedle

1. Navigate to **Settings > Acquisition > Import Folder**.
2. Enable the Import Folder and set the path to your import mount (e.g. `/data/import`).
3. DroppedNeedle will monitor this folder and ingest quiet albums automatically.

### 2. In slskd (`slskd.yml`)

Add the script integration to `slskd.yml`:

```yaml
integration:
  scripts:
    convert_to_opus:
      on:
        - DownloadDirectoryComplete
      run:
        executable: /usr/bin/python3
        args: /scripts/convert.py
        env:
          DROPPEDNEEDLE_IMPORT_DIR: /import
          OPUS_BITRATE: 160k
          CONVERT_LOSSY: "false"
          DELETE_SOURCE: "false"
```

### 3. Docker Volume Sharing

Mount `convert.py` and a shared import folder between `slskd` and `droppedneedle` containers. See `docker-compose.yml.example` for a complete reference.

## Standalone CLI Usage

You can also run the script manually on any folder:

```bash
python3 convert.py "/path/to/downloaded/album" --import-dir "/data/import" --bitrate 160k
```
