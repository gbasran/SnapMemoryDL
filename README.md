Snapchat Memories Downloader

What this is
- A simple Windows app that bulk‑downloads your Snapchat Memories from the exported `memories_history.html`.
- Images and videos save to `snap_memories/images` and `snap_memories/videos`.
- Videos are converted/remuxed so only H.264 MP4s remain (widely compatible).

Quick start (no setup)
- Install Python 3.8+ from python.org and make sure "Add to PATH" is checked.
- Clone or download this repo. The repo already includes `bin/ffmpeg.exe` and `bin/ffprobe.exe`.
- Double‑click `run.bat` (or run `python snap_memories_dl.py`).
- In the app: Click "Select Folder" and choose your exported Snapchat folder (the one that contains the `html` folder and `memories_history.html`). Click "Start".

Notes
- If Snapchat links are expired, regenerate your export. The app follows signed S3 links; expired signatures won’t work.
- This repo includes `bin/ffmpeg.exe` and `bin/ffprobe.exe`, so video conversion to H.264 works out‑of‑the‑box.

Troubleshooting
- If images open as "unsupported format", they were likely HTML error pages. The app now detects that and saves a debug file in `snap_memories\debug` to help diagnose. Re‑generate your export if links are expired.
- To test small batches, you can set the environment variable `SNAP_DL_LIMIT` to a number (e.g., `10`) before launching.

Project layout
- `snap_memories_dl.py` — main app. Auto-installs Python deps on first run.
- `bin/` — includes `ffmpeg.exe` and `ffprobe.exe` used for video conversion.
- `run.bat` — convenience launcher for Windows.
- `requirements.txt` — for developers.
- `build.bat` — optional local EXE builder.
- `.github/workflows/windows-build.yml` — CI to build EXE on tags and upload to Releases.
- `.gitignore` — ignores build artifacts, venvs, and debug outputs.

Selective Downloads & Retry
- Only specific items: Use the “Only indices” box to enter positions (1‑based). Examples:
  - `7` → only item 7
  - `1,5,10` → items 1, 5, and 10
  - `3-8,15,21-25` → ranges and singles
- Retry failed: After a run, click “Retry Failed” to automatically retry just the items that failed.
- Auto backoff: Transient HTTP errors (5xx or 429) are retried up to 3 times with short delays.

License
- This is a simple student project script shared for personal use. No warranty.
