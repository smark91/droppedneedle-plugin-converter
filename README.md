# DroppedNeedle Auto Opus Converter Plugin

A native DroppedNeedle v3 plugin that automatically converts newly landed FLAC downloads into Opus format directly inside your music library.

## Why this exists

In multi-user setups, non-admin users do not have access to underlying download clients like Soulseek (`slskd`). All search requests and downloads must be initiated through DroppedNeedle's web UI.

Because Soulseek primarily hosts FLAC and MP3 files, this plugin listens for completed download events inside DroppedNeedle, converts the lossless files to Opus using the container's built-in FFmpeg, and updates the library catalog so track identities, album associations, and play history remain completely intact.

## How it works

1. Any user searches for an album and clicks **Download** in DroppedNeedle's web UI.
2. DroppedNeedle downloads the release via its configured `slskd` client and lands the files into the library (`/music`).
3. DroppedNeedle emits the `download_completed` event.
4. This plugin intercepts the event in under 50ms and queues it to an internal background worker.
5. The worker locates the landed tracks via DroppedNeedle's database (`library.db`) or file match.
6. The worker invokes `/usr/local/bin/ffmpeg` to transcode FLAC/WAV files to Opus at your configured bitrate (e.g. `160k`), preserving all tags and metadata (`-map_metadata 0`).
7. The worker updates `local_tracks` in `library.db` with the new file format, path, and exact stat revision. DroppedNeedle never treats the tracks as missing or duplicate.
8. The worker removes the source FLAC files.

## Installation

### Method 1. Install from GitHub (Recommended)

1. Push this repository to GitHub (or use your repository URL).
2. In DroppedNeedle, navigate to **Settings > Plugins**.
3. Paste the repository URL into the **Install a plugin** field and click **Preview**.
4. Click **Install**.
5. Enable the plugin on its card, adjust the settings if needed, and click **Save**.

### Method 2. Install by hand (Docker)

Copy the plugin folder into DroppedNeedle's plugins volume:

```bash
cp -r . /path/to/droppedneedle/plugins/auto-opus-converter
```

Then open **Settings > Plugins** in DroppedNeedle, enable the plugin, and click **Save**.

## Plugin Settings

- **Music Library Root (`library_path`)**: Root path of your music library (default: `/music`).
- **Data Cache Path (`cache_path`)**: Path to DroppedNeedle's cache directory where `library.db` is stored (default: `/app/cache`).
- **Target Opus Bitrate (`bitrate`)**: Desired bitrate for the Opus encoder (default: `160k`).
- **Convert Pre-existing Files (`convert_existing`)**: When `false` (default), only newly downloaded releases are converted. Older library files remain untouched. Set to `true` to sweep and convert older lossless files.
- **Delete Original Files (`delete_source`)**: When `true` (default), source lossless files are deleted after successful conversion. Set to `false` to keep original files on disk alongside Opus.
- **Enable Periodic Sweep (`enable_sweep`)**: Periodically check `library.db` every 5 minutes as a safety net for any dropped download events (default: `true`).
- **Convert Lossy Formats (`convert_lossy`)**: Set to `true` if you also want to convert MP3/AAC files to Opus (default: `false`, keeping MP3s as-is to avoid generational audio quality loss).


