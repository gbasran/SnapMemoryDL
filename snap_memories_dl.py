import os, re, threading, shutil, subprocess, sys, json, time
from typing import Optional, Tuple
from pathlib import Path

# Lightweight dependency bootstrap so the script works out-of-the-box
try:
    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    print("Installing required Python packages (requests, beautifulsoup4)...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "beautifulsoup4"])
    except Exception as e:
        print("Failed to install dependencies automatically:", e)
        print("Please run: python -m pip install -r requirements.txt")
        raise
    # Try again
    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Snapchat Memories Downloader"
# Set to None for full runs. You can override per-run by setting the
# environment variable SNAP_DL_LIMIT to a number.
DEBUG_FIRST_N = None

def find_links(html_path: Path):
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    links = soup.find_all("a", string="download")
    items = []
    for link in links:
        href = link.get("href")
        m = re.search(r"'(https://[^']+)'", href or "")
        if not m:
            continue
        url = m.group(1)
        row = link.find_parent("tr")
        tds = row.find_all("td") if row else []
        media_text = tds[1].get_text(strip=True).lower() if len(tds) > 1 else ""
        items.append((url, media_text))
    return items

def _parse_indices(text: str, max_n: int) -> list[int]:
    sel: set[int] = set()
    for part in (text or "").replace(" ", "").split(','):
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a, b = int(a), int(b)
                if a > b:
                    a, b = b, a
                for k in range(a, b + 1):
                    if 1 <= k <= max_n:
                        sel.add(k)
            except Exception:
                continue
        else:
            try:
                k = int(part)
                if 1 <= k <= max_n:
                    sel.add(k)
            except Exception:
                continue
    return sorted(sel)

def _guess_ext_from_headers(r: requests.Response, fallback: str) -> str:
    ctype = r.headers.get("Content-Type", "").lower()
    if "image/jpeg" in ctype:
        return ".jpg"
    if "image/png" in ctype:
        return ".png"
    if "image/heic" in ctype or "image/heif" in ctype:
        return ".heic"
    if "video/mp4" in ctype:
        return ".mp4"
    if "video/quicktime" in ctype:
        return ".mov"
    # Try Content-Disposition filename extension
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename=\"?([^\";]+)\"?", cd)
    if m:
        name = m.group(1)
        suf = os.path.splitext(name)[1]
        if suf:
            return suf
    return fallback

def _ext_from_url(url: str) -> str:
    m = re.search(r"\.([a-z0-9]{2,4})(?:\?|$)", url, re.I)
    if m:
        return "." + m.group(1).lower()
    return ""

def _looks_like_media_contenttype(ctype: str) -> bool:
    ctype = (ctype or "").lower()
    return ctype.startswith("image/") or ctype.startswith("video/") or "octet-stream" in ctype

from typing import Tuple as _TupleType

def _request_with_fallback(session: requests.Session, url: str, attempts: int = 3, log=None) -> requests.Response:
    """Try GET; on 405/403, try POST. Retry on 5xx/429/network errors with backoff."""
    last_exc = None
    for a in range(1, max(1, attempts) + 1):
        try:
            r = session.get(url, timeout=90, allow_redirects=True, stream=True)
            if r.status_code in (405, 403):
                try:
                    r.close()
                except Exception:
                    pass
                r = session.post(url, timeout=90, allow_redirects=True, stream=True)

            if r.status_code >= 500 or r.status_code in (429,):
                if log:
                    log(f"[RETRY] HTTP {r.status_code} for {url} (attempt {a}/{attempts})")
                time.sleep(min(8, 2 ** (a - 1)))
                continue
            return r
        except Exception as e:
            last_exc = e
            if log:
                log(f"[RETRY] Error '{e}' for {url} (attempt {a}/{attempts})")
            time.sleep(min(8, 2 ** (a - 1)))
    if last_exc:
        raise last_exc
    # Shouldn't reach here, but return last response if defined
    return r

def _resolve_media_response(session: requests.Session, url: str, log=None) -> _TupleType[requests.Response, str]:
    """Return a response streaming the media; may follow an intermediate HTML/JSON page.
    Returns (response, final_url).
    """
    r = _request_with_fallback(session, url, attempts=3, log=log)
    if r.status_code >= 400 and r.status_code not in (403, 405):
        return r, r.url
    # If we already have media, return
    if _looks_like_media_contenttype(r.headers.get("Content-Type", "")):
        return r, r.url

    # Try to extract a media URL from JSON or HTML
    content = b""
    try:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                content += chunk
            if len(content) > 1024 * 1024:  # read up to 1MB for parsing
                break
    except Exception:
        pass
    text = content.decode("utf-8", errors="ignore") if content else ""

    # JSON case
    new_url = None
    try:
        data = json.loads(text)
        for k in ("url", "signedUrl", "download_url", "mediaUrl"):
            if isinstance(data, dict) and data.get(k):
                new_url = str(data[k])
                break
    except Exception:
        pass

    if not new_url:
        # HTML/text case: look for https URL to S3 or similar
        m = re.search(r"https://[^\s'\"]+amazonaws[^\s'\"]+", text)
        if m:
            new_url = m.group(0)
        else:
            m2 = re.search(r"https://[^\s'\"]+\.(?:jpg|jpeg|png|mp4|mov)(?:\?[^'\"]*)?", text, re.I)
            if m2:
                new_url = m2.group(0)

    if new_url:
        r.close()
        r2 = _request_with_fallback(session, new_url, attempts=3, log=log)
        return r2, new_url

    # Fallback: return original response (caller will treat as error)
    return r, r.url

def _find_ffmpeg() -> Tuple[Optional[str], Optional[str]]:
    exe = ".exe" if os.name == "nt" else ""
    candidates = []

    # Potential locations: PyInstaller temp, next to script/exe, ./bin
    base_dirs = []
    try:
        if hasattr(sys, "_MEIPASS"):
            base_dirs.append(Path(sys._MEIPASS))
    except Exception:
        pass
    try:
        if getattr(sys, "frozen", False):
            base_dirs.append(Path(sys.executable).parent)
    except Exception:
        pass
    base_dirs += [Path(__file__).parent, Path(__file__).parent / "bin", Path.cwd()]

    for d in base_dirs:
        candidates.append(d / f"ffmpeg{exe}")
        candidates.append(d / f"ffprobe{exe}")
        candidates.append(d / "bin" / f"ffmpeg{exe}")
        candidates.append(d / "bin" / f"ffprobe{exe}")

    def pick(name: str):
        # Prefer local file; fall back to PATH
        for c in candidates:
            if c.name.lower().startswith(name) and c.exists():
                return str(c)
        return shutil.which(name)

    return pick("ffmpeg"), pick("ffprobe")

def _has_ffmpeg() -> bool:
    ffmpeg, ffprobe = _find_ffmpeg()
    return bool(ffmpeg and ffprobe)

def _ensure_ffmpeg_available(log_fn=None) -> bool:
    """Ensure ffmpeg/ffprobe are runnable. If not, download a portable build to ./bin.
    Returns True if available, False otherwise.
    """
    ffmpeg, ffprobe = _find_ffmpeg()
    def log(msg: str):
        try:
            (log_fn or print)(msg)
        except Exception:
            pass
    # Quick sanity check: can we invoke -version?
    try:
        if ffmpeg:
            subprocess.check_call([ffmpeg, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ffprobe:
            subprocess.check_call([ffprobe, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        pass

    # Attempt download on Windows only
    if os.name != "nt":
        return False

    try:
        import zipfile
        url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        bin_dir = Path(__file__).parent / "bin"
        bin_dir.mkdir(exist_ok=True, parents=True)
        tmp_zip = bin_dir / "ffmpeg-release-essentials.zip"
        log("Downloading ffmpeg (first run only)...")
        import requests as _req
        with _req.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(tmp_zip, "wb") as f:
                for chunk in resp.iter_content(1024 * 256):
                    if chunk:
                        f.write(chunk)
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            for name in zf.namelist():
                lname = name.lower()
                if not (lname.endswith("ffmpeg.exe") or lname.endswith("ffprobe.exe") or lname.endswith(".dll")):
                    continue
                # Extract into bin/ preserving just the filename
                target = bin_dir / Path(name).name
                with zf.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
        try:
            tmp_zip.unlink()
        except Exception:
            pass
        # Re-check
        ffmpeg, ffprobe = _find_ffmpeg()
        if ffmpeg:
            subprocess.check_call([ffmpeg, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ffprobe:
            subprocess.check_call([ffprobe, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("ffmpeg ready.")
        return True
    except Exception:
        log("Could not download ffmpeg automatically.")
        return False

def _video_codec(path: Path) -> str:
    try:
        _, ffprobe = _find_ffmpeg()
        if not ffprobe:
            return ""
        out = subprocess.check_output([
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=nw=1:nk=1",
            str(path)
        ], stderr=subprocess.STDOUT, text=True).strip()
        return out
    except Exception:
        return ""

def _convert_to_h264_mp4(src: Path, dst: Path, copy_if_h264: bool = True) -> bool:
    try:
        ffmpeg, _ = _find_ffmpeg()
        if not ffmpeg:
            return False
        vcodec = _video_codec(src).lower()
        # If already H.264 and caller allows copy, just remux to mp4
        if copy_if_h264 and vcodec in {"h264", "avc1"}:
            cmd = [ffmpeg, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
        else:
            cmd = [ffmpeg, "-y", "-i", str(src),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                   "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def _ensure_h264_mp4(src_path: Path, log) -> Optional[Path]:
    """Create an H.264 MP4 from src_path if needed and return the final path.
    Always writes to a different temp file to avoid reading/writing same file.
    Deletes the original file on success.
    """
    if not _has_ffmpeg():
        return None
    final_path = src_path.with_suffix('.mp4')
    # Always write to temp to avoid ffmpeg input/output same file
    temp_path = final_path.parent / (final_path.stem + "_tmpconv.mp4")

    if temp_path.exists():
        try:
            temp_path.unlink()
        except Exception:
            pass

    ok = _convert_to_h264_mp4(src_path, temp_path)
    if not ok:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        return None

    # Replace/rename temp to final
    try:
        if final_path.exists():
            final_path.unlink()
        temp_path.replace(final_path)
        # Remove original
        if src_path.exists() and src_path.resolve() != final_path.resolve():
            src_path.unlink()
        return final_path
    except Exception as e:
        log(f"[WARN] Failed to move converted file: {e}")
        return None

def download_all(root_dir: Path, log, bar, start_btn, stop_btn, stop_event: threading.Event, limit: Optional[int] = None, indices_text: Optional[str] = None, failed_out: Optional[list] = None, retry_btn=None):
    try:
        html_file = root_dir / "html" / "memories_history.html"
        if not html_file.exists():
            messagebox.showerror(APP_TITLE, f"Couldn't find:\n{html_file}")
            return

        out_root = root_dir / "snap_memories"
        imgs = out_root / "images"
        vids = out_root / "videos"
        imgs.mkdir(parents=True, exist_ok=True)
        vids.mkdir(parents=True, exist_ok=True)

        items = find_links(html_file)
        total_all = len(items)
        # Determine selected indices
        selected_indices: list[int] = []
        if indices_text:
            selected_indices = _parse_indices(indices_text, total_all)
        # Apply optional global limit if no specific selection
        if not selected_indices and limit:
            items = items[:limit]
        # Build work list of (index, (url, media_text))
        if selected_indices:
            work = [(idx, items[idx - 1]) for idx in selected_indices if 1 <= idx <= total_all]
        else:
            work = list(enumerate(items, start=1))
        total = len(work)
        if total == 0:
            messagebox.showinfo(APP_TITLE, "No download links found in this export.")
            return

        bar["value"] = 0
        bar["maximum"] = total
        log(f"Found {total} memories. Saving to:\n{out_root}\n")

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
            ,"Accept": "image/*,video/*;q=0.9,*/*;q=0.5"
            ,"Referer": "https://app.snapchat.com/"
        })

        ok = 0
        fail = 0
        if isinstance(failed_out, list):
            failed_out.clear()
        for i, (idx_item) in enumerate(work, start=1):
            idx, (url, media_text) = idx_item
            if stop_event.is_set():
                log("[STOP] Stopping before next item...")
                break
            is_video = "video" in media_text
            folder = vids if is_video else imgs
            default_ext = ".mp4" if is_video else ".jpg"
            dest = folder / f"memory_{idx}{default_ext}"
            try:
                r, final_url = _resolve_media_response(session, url)
                if r.status_code >= 400:
                    debug_dir = out_root / "debug"; debug_dir.mkdir(exist_ok=True, parents=True)
                    dbg = debug_dir / f"response_{i}_status{r.status_code}.txt"
                    try:
                        text = r.text
                    except Exception:
                        text = ""
                    dbg.write_text(text, encoding="utf-8", errors="ignore")
                    r.raise_for_status()
                # Validate response really is media
                ctype = r.headers.get("Content-Type", "").lower()
                if not _looks_like_media_contenttype(ctype):
                    debug_dir = out_root / "debug"
                    debug_dir.mkdir(exist_ok=True, parents=True)
                    dbg = debug_dir / f"response_{i}.html"
                    try:
                        # Try to decode small sample
                        sample = next(r.iter_content(1024 * 128))
                        text = sample.decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    dbg.write_text(text, encoding="utf-8", errors="ignore")
                    raise RuntimeError(f"Unexpected content type: {ctype}. Saved sample to {dbg}")

                # Use headers or URL to decide correct extension
                ext = _guess_ext_from_headers(r, default_ext)
                if ext in (".bin", ""):
                    from_url = _ext_from_url(final_url)
                    if from_url:
                        ext = from_url
                if dest.suffix.lower() != ext.lower():
                    dest = dest.with_suffix(ext)
                # Save streaming to avoid corruption
                with dest.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if stop_event.is_set():
                            log("[STOP] Cancelled during download; partial file removed.")
                            try:
                                f.close()
                                if dest.exists():
                                    dest.unlink()
                            except Exception:
                                pass
                            raise RuntimeError("Stopped by user")
                        if chunk:
                            f.write(chunk)
                ok += 1
                log(f"[OK] #{idx} -> {dest.name}")

                # Ensure only H.264 MP4 remains for videos
                if is_video:
                    final_path = _ensure_h264_mp4(dest, log)
                    if final_path is not None:
                        dest = final_path
                        log(f"[H264] {dest.name}")
                    else:
                        if not _has_ffmpeg():
                            log("[INFO] ffmpeg not found; cannot guarantee H.264 only.")
                        else:
                            log(f"[WARN] ffmpeg failed to create H.264 for {dest.name}; keeping original.")
            except Exception as e:
                fail += 1
                if isinstance(failed_out, list):
                    failed_out.append(idx)
                log(f"[FAIL] #{idx} {url} -> {e}")

            bar["value"] = i
            bar.update_idletasks()

        status = "Stopped" if stop_event.is_set() else "Done"
        log(f"\n{status}. {ok} downloaded, {fail} failed.")
        if failed_out and len(failed_out) > 0:
            log(f"Failed indices: {failed_out}")
        msg = "Stopped early.\n" if stop_event.is_set() else "Finished.\n"
        msg += f"Images: {imgs}\n"
        msg += f"Videos: {vids}\n"
        messagebox.showinfo(APP_TITLE, msg)
    finally:
        start_btn.config(state="normal")
        try:
            stop_btn.config(state="disabled")
        except Exception:
            pass
        try:
            if retry_btn is not None and failed_out is not None and len(failed_out) > 0:
                retry_btn.config(state="normal")
            elif retry_btn is not None:
                retry_btn.config(state="disabled")
        except Exception:
            pass

def pick_folder_only(txt_var):
    chosen = filedialog.askdirectory(title="Select your mydata~... folder (the one with /html)")
    if not chosen:
        return
    p = Path(chosen)
    txt_var.set(str(p))

def main():
    win = tk.Tk()
    win.title(APP_TITLE)
    win.geometry("640x480")

    info = tk.Label(win, text="1) Click Select Folder\n2) Choose your exported Snapchat 'mydata~...' folder\n3) Click Start", justify="left")
    info.pack(pady=8, anchor="w", padx=10)

    path_var = tk.StringVar()
    path_entry = tk.Entry(win, textvariable=path_var, width=80)
    path_entry.pack(padx=10, fill="x")

    frame = tk.Frame(win)
    frame.pack(padx=10, pady=6, fill="x")
    log_box = tk.Text(win, height=18, wrap="word")
    log_box.pack(padx=10, pady=6, fill="both", expand=True)

    def log(msg):
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    bar = ttk.Progressbar(win, orient="horizontal", mode="determinate")
    bar.pack(padx=10, pady=6, fill="x")

    btns = tk.Frame(win); btns.pack(padx=10, pady=8, fill="x")
    # Buttons are created first; actions wired below so closures can see them
    start_btn = tk.Button(btns, text="Start")
    stop_btn = tk.Button(btns, text="Stop")
    retry_btn = tk.Button(btns, text="Retry Failed")
    select_btn = tk.Button(btns, text="Select Folder", command=lambda: pick_folder_only(path_var))
    select_btn.pack(side="left")

    # Index selection entry
    idx_var = tk.StringVar()
    idx_entry = tk.Entry(btns, textvariable=idx_var, width=18)
    idx_entry.insert(0, "Only indices (e.g. 1,5-8)")
    def _clear_placeholder(event):
        if idx_var.get().startswith("Only indices"):
            idx_var.set("")
    idx_entry.bind("<FocusIn>", _clear_placeholder)
    idx_entry.pack(side="left", padx=8)

    # Shared state for run control
    state = {"thread": None, "stop_event": None, "failed": [], "last_root": None}

    def start_from_path():
        p = Path(path_var.get().strip())
        if not p.exists():
            messagebox.showerror(APP_TITLE, "Please select a valid folder.")
            return
        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        state["stop_event"] = threading.Event()
        state["failed"] = []
        state["last_root"] = p
        # Optional per-run limit (via env var SNAP_DL_LIMIT)
        limit_env = os.environ.get("SNAP_DL_LIMIT")
        try:
            limit_val = int(limit_env) if limit_env else DEBUG_FIRST_N
        except Exception:
            limit_val = DEBUG_FIRST_N
        t = threading.Thread(target=download_all, args=(p, log, bar, start_btn, stop_btn, state["stop_event"], limit_val, idx_var.get().strip(), state["failed"], retry_btn), daemon=True)
        state["thread"] = t
        t.start()

    def stop_current():
        if state.get("stop_event"):
            state["stop_event"].set()
        stop_btn.config(state="disabled")

    def retry_failed():
        if not state.get("failed"):
            messagebox.showinfo(APP_TITLE, "No failed indices to retry.")
            return
        if not state.get("last_root"):
            messagebox.showerror(APP_TITLE, "No previous folder selected.")
            return
        indices_text = ",".join(str(i) for i in sorted(set(state["failed"])))
        idx_var.set(indices_text)
        start_from_path()

    start_btn.config(command=start_from_path)
    start_btn.pack(side="left", padx=8)
    stop_btn.config(command=stop_current, state="disabled")
    stop_btn.pack(side="left", padx=8)
    retry_btn.config(command=retry_failed, state="disabled")
    retry_btn.pack(side="left", padx=8)

    quit_btn = tk.Button(btns, text="Quit", command=win.destroy)
    quit_btn.pack(side="right")

    win.mainloop()

if __name__ == "__main__":
    main()
