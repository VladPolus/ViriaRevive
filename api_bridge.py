"""
ApiBridge  –  Python ↔ JavaScript bridge for the ViriaRevive GUI.

Every public method is exposed to the frontend as  pywebview.api.<method>().
Long-running work runs on a daemon thread; progress is pushed back to
the UI via  window.evaluate_js()  which calls global JS callback functions.
"""

import functools
import http.server
import json
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yt_dlp

from config import (
    BASE_DIR,
    CLIPS_DIR,
    CLIP_DURATION,
    CROP_VERTICAL,
    DOWNLOADS_DIR,
    FFMPEG_PRESET,
    MIN_GAP,
    MUSIC_DIR,
    NUM_CLIPS,
    SUBTITLE_STYLE,
    SUBTITLES_DIR,
    VIDEO_CRF,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)
from detector import find_viral_moments
from transcriber import transcribe_clip, find_sentence_boundary
from subtitler import generate_subtitles, get_available_styles
from clipper import (
    extract_clip, extract_audio_clip, ClipResult,
    add_background_music, apply_video_effect, get_effects_list,
)
from cropper import get_crop_params, get_crop_params_dynamic, get_dimensions
from subprocess_utils import CancelledError
from title_generator import generate_title, generate_titles_batch, list_ollama_models, ensure_model
from uploader import (
    upload_to_youtube,
    build_schedule,
    get_youtube_service,
    is_connected,
    disconnect,
    list_channels,
    list_categories,
    add_account,
    list_accounts,
)

STATE_FILE = BASE_DIR / "viria_state.json"


# ── Log interceptor — captures print() and forwards to the GUI console ───────

import sys as _sys
import io as _io


class _LogTee:
    """Wraps stdout/stderr: writes to both the original stream and a callback."""

    def __init__(self, original, callback):
        self._orig = original
        self._cb = callback
        self._encoding = getattr(original, 'encoding', 'utf-8')

    def write(self, text):
        try:
            self._orig.write(text)
        except (UnicodeEncodeError, UnicodeDecodeError):
            # Windows console can't handle some Unicode chars — strip them
            safe = text.encode('ascii', errors='replace').decode('ascii')
            try:
                self._orig.write(safe)
            except Exception:
                pass
        if text and text.strip():
            try:
                self._cb(text.strip())
            except Exception:
                pass
        return len(text)

    def flush(self):
        self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


_log_bridge = None  # set by ApiBridge.__init__


def _install_log_tee():
    """Install stdout/stderr tee that pushes logs to the frontend console."""
    _forwarding = threading.local()

    def _forward(text):
        # Guard against recursion (if evaluate_js triggers a print)
        if getattr(_forwarding, 'active', False):
            return
        _forwarding.active = True
        try:
            if _log_bridge and _log_bridge._window:
                escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
                _log_bridge._js(f"window.onConsoleLog(`{escaped}`)")
        finally:
            _forwarding.active = False

    _sys.stdout = _LogTee(_sys.__stdout__ or _sys.stdout, _forward)
    _sys.stderr = _LogTee(_sys.__stderr__ or _sys.stderr, _forward)


# ── Local video server (serves clip files for HTML5 <video> preview) ─────────

class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files from a directory with CORS headers, no logging."""

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # Browser closed connection early — harmless


class _SilentHTTPServer(http.server.HTTPServer):
    """HTTPServer that suppresses broken-pipe / connection-reset tracebacks."""

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, OSError)):
            return  # browser closed connection early — harmless
        super().handle_error(request, client_address)


def _start_video_server(clips_dir: Path) -> int:
    """Start a local HTTP server for video previews; returns the port."""
    handler = functools.partial(_SilentHandler, directory=str(clips_dir))
    # Bind to port 0 → OS picks a free port
    server = _SilentHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[+] Video preview server on http://127.0.0.1:{port}")
    return port


class ApiBridge:
    def __init__(self):
        self._window = None
        self._processing = False
        self._cancel = False
        self._results: list[Path] = []
        self._moments: list[dict] = []
        self._scheduled: list[dict] = []
        self._video_port = _start_video_server(CLIPS_DIR)
        self._music_port = _start_video_server(MUSIC_DIR)
        self._scheduler_running = False
        self._delete_after_upload = False   # auto-delete clips after YouTube upload
        self._user_settings: dict = {}      # user settings persisted to disk
        self._pending_js: list[str] = []    # JS calls queued while window was hidden

        # Install log interceptor so print() output goes to the GUI console
        global _log_bridge
        _log_bridge = self
        _install_log_tee()

        # Load persisted state from previous session
        self._load_state()

    # ── Exposed: config / deps ───────────────────────────────────────────

    def get_settings(self):
        """Return user settings (persisted overrides merged with defaults)."""
        defaults = {
            "num_clips": NUM_CLIPS,
            "clip_duration": CLIP_DURATION,
            "min_gap": MIN_GAP,
            "whisper_model": WHISPER_MODEL,
            "whisper_language": WHISPER_LANGUAGE or "",
            "subtitle_style": SUBTITLE_STYLE,
            "ffmpeg_preset": FFMPEG_PRESET,
            "video_crf": VIDEO_CRF,
            "crop_vertical": CROP_VERTICAL,
        }
        # Merge saved user overrides (from save_settings)
        if self._user_settings:
            defaults.update(self._user_settings)
        return defaults

    def save_settings(self, settings):
        """Persist user settings to disk so they survive restarts."""
        self._user_settings = settings or {}
        self._save_state()
        return {"ok": True}

    def check_dependencies(self):
        return {"ffmpeg": shutil.which("ffmpeg") is not None}

    def set_delete_after_upload(self, enabled):
        """Toggle auto-delete clips from disk after successful YouTube upload."""
        self._delete_after_upload = bool(enabled)
        return {"ok": True, "enabled": self._delete_after_upload}

    def get_delete_after_upload(self):
        return {"enabled": self._delete_after_upload}

    # ── Exposed: AI title generation ──────────────────────────────────────

    def generate_titles(self):
        """Generate titles for all clips using LLM (or heuristic fallback).

        If transcripts are missing (e.g. clips from a previous session where
        moments were lost), auto-transcribe the clip audio first.
        """
        from title_generator import ensure_model, DEFAULT_MODEL

        num_clips = len(self._results)
        # Sync moments to match results count exactly
        if len(self._moments) > num_clips:
            self._moments = self._moments[:num_clips]
        while len(self._moments) < num_clips:
            self._moments.append({})

        # Backfill any clips missing transcripts
        missing = [i for i in range(num_clips)
                   if not self._moments[i].get("transcript")]
        if missing:
            for i in missing:
                self._backfill_transcript_single(i)
            self._save_state()

        transcripts = [m.get("transcript", "") for m in self._moments]
        if not any(transcripts):
            return {"titles": [], "error": "No transcripts available — process clips first"}

        llm_available = ensure_model(DEFAULT_MODEL)
        titles = generate_titles_batch(transcripts)
        return {"titles": titles, "llm": llm_available}

    def generate_title_for_clip(self, clip_index):
        """Generate a title for a single clip."""
        # Ensure moments list matches results length
        while len(self._moments) < len(self._results):
            self._moments.append({})

        if clip_index < 0 or clip_index >= len(self._moments):
            return {"title": "", "error": "Invalid clip index"}

        transcript = self._moments[clip_index].get("transcript", "")

        # If no transcript, try to transcribe from the clip file
        if not transcript and clip_index < len(self._results):
            self._backfill_transcript_single(clip_index)
            transcript = self._moments[clip_index].get("transcript", "")

        if not transcript:
            return {"title": "", "error": "No transcript for this clip"}
        title = generate_title(transcript)
        return {"title": title}

    def rename_clip(self, clip_index, new_title):
        """Rename a clip file on disk to match a new title.

        Returns the new filename, or error.
        """
        if clip_index < 0 or clip_index >= len(self._results):
            return {"error": "Invalid clip index"}
        old_path = self._results[clip_index]
        if not old_path.exists():
            return {"error": "File not found"}

        # Sanitize title for filesystem
        import re
        # Remove emojis and non-ASCII chars that cause issues on Windows
        safe = re.sub(r'[^\x20-\x7E]', '', new_title)
        safe = re.sub(r'[<>:"/\\|?*]', '', safe)
        safe = safe.strip('. ')[:80]
        if not safe:
            return {"error": "Title too short after sanitization"}

        ext = old_path.suffix
        new_name = f"{safe}{ext}"
        new_path = old_path.parent / new_name

        # Avoid collisions
        if new_path.exists() and new_path != old_path:
            counter = 2
            while new_path.exists():
                new_name = f"{safe} ({counter}){ext}"
                new_path = old_path.parent / new_name
                counter += 1

        try:
            old_path.rename(new_path)
            self._results[clip_index] = new_path
            self._save_state()
            print(f"[rename] {old_path.name} → {new_path.name}")
            return {"filename": new_path.name, "path": str(new_path)}
        except Exception as e:
            return {"error": str(e)}

    def generate_and_rename_all(self):
        """Generate AI titles for all clips in a background thread.

        Returns immediately with {"ok": True}. Progress and results are
        pushed to the frontend via window.onTitleProgress and
        window.onTitlesDone callbacks.
        """
        threading.Thread(target=self._run_title_gen, daemon=True).start()
        return {"ok": True}

    def generate_and_rename_indices(self, indices):
        """Generate AI titles only for specific clip indices (e.g. a folder).

        Returns immediately with {"ok": True}. Progress and results are
        pushed to the frontend via window.onTitleProgress and
        window.onTitlesDone callbacks.
        """
        threading.Thread(target=self._run_title_gen, args=(indices,), daemon=True).start()
        return {"ok": True}

    def _run_title_gen(self, only_indices=None):
        """Background thread: generate titles, rename files, push results to JS.

        If only_indices is provided (list of ints), only those clip indices
        are transcribed and titled. Otherwise all clips are processed.
        """
        try:
            from title_generator import ensure_model, DEFAULT_MODEL

            num_clips = len(self._results)
            print(f"[title-gen] {num_clips} clips, {len(self._moments)} moments in state")

            # Trim moments to match results (moments can accumulate beyond results
            # if clips were deleted or state got out of sync)
            if len(self._moments) > num_clips:
                self._moments = self._moments[:num_clips]
            # Pad if fewer
            while len(self._moments) < num_clips:
                self._moments.append({})

            # Determine which indices to process
            target_indices = only_indices if only_indices is not None else list(range(num_clips))
            # Filter to valid range
            target_indices = [i for i in target_indices if 0 <= i < num_clips]
            if not target_indices:
                self._js("window.onTitlesDone && window.onTitlesDone({error: 'No valid clips to process'})")
                return

            print(f"[title-gen] Processing {len(target_indices)} of {num_clips} clips")

            # Backfill any target clips missing transcripts
            missing = [i for i in target_indices
                       if not self._moments[i].get("transcript")]
            if missing:
                print(f"[title-gen] {len(missing)} clips missing transcripts, backfilling...")
                for idx, i in enumerate(missing):
                    self._js(f"window.onTitleProgress && window.onTitleProgress({idx}, {len(missing)}, 'Transcribing clip {i+1}...')")
                    self._backfill_transcript_single(i)
                self._save_state()

            # Build transcripts list — only for target indices, empty for others
            transcripts = [""] * num_clips
            for i in target_indices:
                transcripts[i] = self._moments[i].get("transcript", "")
            if not any(transcripts[i] for i in target_indices):
                self._js("window.onTitlesDone && window.onTitlesDone({error: 'No transcripts available'})")
                return

            # Store original stem before renaming
            import re
            for i in target_indices:
                p = self._results[i]
                if i < len(self._moments) and not self._moments[i].get("source_stem"):
                    m = re.match(r'^(.+?)_viral\d+', p.name)
                    self._moments[i]["source_stem"] = m.group(1) if m else p.stem

            llm_available = ensure_model(DEFAULT_MODEL)

            def _on_progress(done, total, title):
                self._js(f"window.onTitleProgress && window.onTitleProgress({done}, {total}, `{self._esc(title or '')}`)")

            titles = generate_titles_batch(
                transcripts, DEFAULT_MODEL, on_progress=_on_progress
            )

            renamed = 0
            results = []
            for i in target_indices:
                title = titles[i] if i < len(titles) else ""
                if not title:
                    results.append({"index": i, "title": "", "renamed": False})
                    continue
                r = self.rename_clip(i, title)
                ok = "filename" in r
                if ok:
                    renamed += 1
                results.append({
                    "index": i,
                    "title": title,
                    "renamed": ok,
                    "filename": r.get("filename", self._results[i].name if i < len(self._results) else ""),
                })

            self._save_state()

            # Push results to frontend
            import json
            payload = json.dumps({"titles": results, "renamed": renamed, "llm": llm_available, "total": len(titles)})
            self._js(f"window.onTitlesDone && window.onTitlesDone({payload})")

        except Exception as e:
            print(f"[title-gen] Error: {e}")
            self._js(f"window.onTitlesDone && window.onTitlesDone({{error: `{self._esc(str(e))}`}})")

    def _backfill_transcripts(self):
        """Transcribe clips that are missing transcripts (e.g. from previous sessions)."""
        print("[title-gen] Backfilling missing transcripts from clip audio...")
        for i, p in enumerate(self._results):
            if i < len(self._moments) and self._moments[i].get("transcript"):
                continue  # already has transcript
            self._backfill_transcript_single(i)
        self._save_state()  # persist backfilled transcripts

    def _backfill_transcript_single(self, clip_index):
        """Transcribe a single clip to fill in its transcript."""
        import tempfile
        if clip_index >= len(self._results):
            return
        p = self._results[clip_index]
        if not p.exists():
            return

        # Ensure moments slot exists
        while len(self._moments) <= clip_index:
            self._moments.append({})

        try:
            wav = Path(tempfile.gettempdir()) / f"viria_backfill_{clip_index}.wav"
            extract_audio_clip(p, 0, 60, wav)  # max 60s
            if wav.exists() and wav.stat().st_size > 1000:
                words = transcribe_clip(wav, model_size=WHISPER_MODEL, language=None)
                transcript = " ".join(w.get("text", "") for w in words).strip()
                if transcript:
                    self._moments[clip_index]["transcript"] = transcript
                    print(f"  [+] Clip {clip_index + 1}: {len(transcript)} chars transcribed")
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception as e:
            print(f"  [!] Backfill failed for clip {clip_index + 1}: {e}")

    def get_ollama_models(self):
        """Return available Ollama models for title generation."""
        models = list_ollama_models()
        return {"models": models, "available": len(models) > 0}

    def ensure_ollama_model(self, model=None):
        """Ensure the title generation model is downloaded. Auto-pulls if needed."""
        from title_generator import DEFAULT_MODEL
        model = model or DEFAULT_MODEL
        ready = ensure_model(model)
        return {"ready": ready, "model": model}


    # ── Exposed: YouTube connection ───────────────────────────────────────

    def connect_youtube(self):
        """Add a YouTube account via OAuth flow. Supports multiple accounts."""
        try:
            result = add_account()
            return {"ok": True, "account": result}
        except FileNotFoundError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"Connection failed: {e}"}

    def add_youtube_account(self):
        """Alias for connect_youtube — adds another account."""
        return self.connect_youtube()

    def disconnect_youtube(self, account_id=None):
        """Disconnect a specific account, or all accounts if no ID given."""
        disconnect(account_id)
        return {"ok": True}

    def youtube_status(self):
        return {"connected": is_connected(), "accounts": list_accounts()}

    def get_channels(self):
        try:
            return {"channels": list_channels()}
        except Exception as e:
            return {"error": str(e), "channels": []}

    def get_categories(self):
        try:
            return {"categories": list_categories()}
        except Exception as e:
            return {"error": str(e), "categories": []}

    def get_subtitle_styles(self):
        """Return available subtitle styles for the UI picker."""
        return {"styles": get_available_styles()}

    def get_effects(self):
        """Return available video effect presets."""
        return {"effects": get_effects_list()}

    def list_music(self):
        """List audio files in the music/ folder."""
        tracks = []
        if MUSIC_DIR.exists():
            for p in sorted(MUSIC_DIR.iterdir()):
                if p.suffix.lower() in ('.mp3', '.wav', '.aac', '.ogg', '.m4a', '.flac'):
                    tracks.append({
                        "filename": p.name,
                        "path": str(p),
                        "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
                    })
        return {"tracks": tracks, "music_dir": str(MUSIC_DIR)}

    def get_music_url(self, filename):
        """Return a local HTTP URL for a music file so the browser can play it."""
        music_path = MUSIC_DIR / filename
        if music_path.exists():
            return {"url": f"http://127.0.0.1:{self._music_port}/{filename}"}
        return {"url": None}

    def open_music_folder(self):
        """Open the music folder in system explorer."""
        MUSIC_DIR.mkdir(exist_ok=True)
        try:
            os.startfile(str(MUSIC_DIR))
        except Exception:
            pass
        return {"ok": True}

    def get_music_waveform(self, filename):
        """Generate waveform data + duration for a music file.

        Returns {peaks: [...], duration: float} where peaks is ~200 normalized
        amplitude values (0.0-1.0) representing the waveform shape.
        """
        from subprocess_utils import run as _run
        music_path = MUSIC_DIR / filename
        if not music_path.exists():
            return {"error": "File not found", "peaks": [], "duration": 0}

        try:
            # Get duration
            dr = _run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(music_path)],
                capture_output=True, text=True, timeout=15,
            )
            duration = float(dr.stdout.strip())

            # Extract raw PCM samples at low sample rate for waveform
            # 200 peaks over the full duration → sample_rate ~ 200/duration
            num_peaks = 200
            sample_rate = max(100, int(num_peaks / max(duration, 0.1)))

            pr = _run(
                ["ffmpeg", "-y", "-i", str(music_path),
                 "-ac", "1",  # mono
                 "-ar", str(sample_rate),  # low sample rate
                 "-f", "s16le",  # raw 16-bit PCM
                 "-"],
                capture_output=True, timeout=30,
            )

            if pr.returncode != 0:
                return {"error": "Failed to read audio", "peaks": [], "duration": duration}

            import struct
            raw = pr.stdout
            # Parse 16-bit signed samples
            n_samples = len(raw) // 2
            if n_samples == 0:
                return {"peaks": [], "duration": duration}

            samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])

            # Bucket into num_peaks groups and take max absolute amplitude
            bucket_size = max(1, n_samples // num_peaks)
            peaks = []
            for i in range(0, n_samples, bucket_size):
                bucket = samples[i:i + bucket_size]
                peak = max(abs(s) for s in bucket) / 32768.0
                peaks.append(round(peak, 3))

            # Trim or pad to exactly num_peaks
            peaks = peaks[:num_peaks]

            return {"peaks": peaks, "duration": round(duration, 2)}

        except Exception as e:
            return {"error": str(e), "peaks": [], "duration": 0}

    # ── Exposed: processing ──────────────────────────────────────────────

    def start_processing(self, url, settings):
        if self._processing:
            return {"error": "Already processing"}
        self._processing = True
        self._cancel = False
        from subprocess_utils import reset_cancel
        reset_cancel()
        # Store pre-existing results count so _run_pipeline appends instead of replacing
        self._results_before = len(self._results)
        threading.Thread(target=self._run_pipeline, args=(url, settings), daemon=True).start()
        return {"ok": True}

    def cancel_processing(self):
        self._cancel = True
        from subprocess_utils import request_cancel
        request_cancel()
        return {"ok": True}

    # ── Exposed: results ─────────────────────────────────────────────────

    def get_results(self):
        clips = []
        for i, p in enumerate(self._results):
            clip = {
                "path": str(p),
                "filename": p.name,
                "size_mb": round(p.stat().st_size / (1024 * 1024), 1) if p.exists() else 0,
                "url": f"http://127.0.0.1:{self._video_port}/{p.name}" if p.exists() else "",
            }
            # Include source_stem for grouping renamed clips
            if i < len(self._moments) and self._moments[i].get("source_stem"):
                clip["source_stem"] = self._moments[i]["source_stem"]
            clips.append(clip)
        return {"clips": clips, "moments": self._moments}

    def open_output_folder(self):
        try:
            os.startfile(str(CLIPS_DIR))
        except Exception:
            pass
        return {"ok": True}

    def select_file(self):
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Video files (*.mp4;*.mkv;*.avi;*.mov;*.webm)", "All files (*.*)"),
        )
        if result and len(result) > 0:
            return {"path": result[0]}
        return {"path": None}

    def select_files_multiple(self):
        """Open file dialog allowing multiple file selection."""
        import webview

        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Video files (*.mp4;*.mkv;*.avi;*.mov;*.webm)", "All files (*.*)"),
            allow_multiple=True,
        )
        if result and len(result) > 0:
            return {"paths": list(result)}
        return {"paths": []}

    # ── Exposed: video preview ───────────────────────────────────────────

    def get_video_url(self, clip_index):
        """Return a local HTTP URL for the clip so the HTML5 <video> can play it."""
        if 0 <= clip_index < len(self._results):
            p = self._results[clip_index]
            if p.exists():
                return {"url": f"http://127.0.0.1:{self._video_port}/{p.name}"}
        return {"url": None}

    # ── Exposed: delete clip ────────────────────────────────────────────

    def delete_clip(self, clip_index):
        """Delete a clip by its index in the current results list."""
        if 0 <= clip_index < len(self._results):
            p = self._results[clip_index]
            try:
                if p.exists():
                    p.unlink()
                self._results.pop(clip_index)
                # Remove matching moments entry
                if clip_index < len(self._moments):
                    self._moments.pop(clip_index)
                self._save_state()
                return {"ok": True}
            except Exception as e:
                return {"error": str(e)}
        return {"error": "Invalid clip index"}

    def delete_library_file(self, filename):
        """Delete a video file from the clips folder by filename."""
        target = CLIPS_DIR / filename
        if target.exists() and target.parent == CLIPS_DIR:
            try:
                target.unlink()
                # Also remove from results if it was there
                self._results = [p for p in self._results if p.name != filename]
                self._save_state()
                return {"ok": True}
            except Exception as e:
                return {"error": str(e)}
        return {"error": "File not found"}

    # ── Exposed: library (all videos) ────────────────────────────────────

    def list_all_clips(self):
        """List all video files in the clips directory."""
        clips = []
        total_size = 0
        _exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
        if CLIPS_DIR.exists():
            # Single stat() per file — cache the result
            entries = []
            for p in CLIPS_DIR.iterdir():
                if p.suffix.lower() in _exts:
                    st = p.stat()
                    entries.append((p, st))
            entries.sort(key=lambda x: x[1].st_mtime, reverse=True)
            for p, st in entries:
                total_size += st.st_size
                clips.append({
                    "filename": p.name,
                    "size_mb": round(st.st_size / (1024 * 1024), 1),
                    "modified": st.st_mtime,
                    "url": f"http://127.0.0.1:{self._video_port}/{p.name}",
                })
        return {
            "clips": clips,
            "total_size_mb": round(total_size / (1024 * 1024), 1),
            "count": len(clips),
        }

    def import_folder_clips(self):
        """Scan the clips folder and add any videos not already tracked.

        This lets users drop videos into the clips/ folder and have them
        appear in the upload section alongside pipeline-generated clips.
        Returns the updated results list.
        """
        _exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
        existing = {p.resolve() for p in self._results if p.exists()}
        added = 0

        if CLIPS_DIR.exists():
            for p in sorted(CLIPS_DIR.iterdir(), key=lambda x: x.stat().st_mtime):
                if p.suffix.lower() in _exts and p.resolve() not in existing:
                    self._results.append(p)
                    existing.add(p.resolve())
                    added += 1

        if added:
            self._save_state()
            print(f"[+] Imported {added} clip(s) from clips folder")

        return self.get_results()

    # ── Exposed: schedule management ─────────────────────────────────────

    def save_scheduled(self, scheduled_list):
        """Replace the full scheduled list (called from JS on every change)."""
        self._scheduled = scheduled_list or []
        self._save_state()
        return {"ok": True}

    def get_all_scheduled(self):
        """Return the persisted scheduled list."""
        return {"scheduled": self._scheduled}

    # ── Exposed: upload ──────────────────────────────────────────────────

    def start_upload(self, clips_metadata, schedule_start, interval_hours, channel_id=None):
        """Upload clips with per-clip metadata.

        clips_metadata: list of {index, title, description, tags, category_id, privacy}
        channel_id: YouTube channel ID to upload to (from get_channels())
        """
        if self._processing:
            return {"error": "Processing in progress"}
        self._processing = True
        self._cancel = False
        threading.Thread(
            target=self._run_upload,
            args=(clips_metadata, schedule_start, interval_hours, channel_id),
            daemon=True,
        ).start()
        return {"ok": True}

    def upload_single_clip(self, clip_index, meta, channel_id=None):
        """Upload a single clip immediately (used by background scheduler)."""
        if clip_index >= len(self._results):
            return {"error": "Invalid clip index"}
        video_path = self._results[clip_index]
        if not video_path.exists():
            return {"error": "Clip file not found"}
        try:
            upload_to_youtube(
                video_path,
                title=meta.get("title", f"Viral Clip #{clip_index + 1}"),
                description=meta.get("description", ""),
                tags=meta.get("tags", ["shorts", "viral", "clips"]),
                category_id=str(meta.get("category_id", "22")),
                privacy=meta.get("privacy", "private"),
                channel_id=channel_id,
            )
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    # ── Exposed: background scheduler ────────────────────────────────────

    def start_scheduler(self):
        """Start the background upload scheduler thread."""
        if self._scheduler_running:
            return {"ok": True}
        self._scheduler_running = True
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        print("[+] Background upload scheduler started")
        return {"ok": True}

    # ── Exposed: state persistence ───────────────────────────────────────

    def load_persisted_state(self):
        """Return persisted results/moments/scheduled for frontend init."""
        clips = []
        for i, p in enumerate(self._results):
            if not p.exists():
                continue
            clip = {
                "path": str(p),
                "filename": p.name,
                "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
            }
            # Include source_stem so frontend can group renamed clips by source video
            if i < len(self._moments) and self._moments[i].get("source_stem"):
                clip["source_stem"] = self._moments[i]["source_stem"]
            clips.append(clip)
        return {
            "clips": clips,
            "moments": self._moments[:len(self._results)],
            "scheduled": self._scheduled,
        }

    # ── Pipeline orchestrator (background thread) ────────────────────────

    def _run_pipeline(self, url, settings):
        try:
            num_clips_raw = settings.get("num_clips", NUM_CLIPS)
            auto_clips = num_clips_raw == "auto"
            num_clips = NUM_CLIPS if auto_clips else int(num_clips_raw)
            print(f"[*] Pipeline settings: num_clips_raw={num_clips_raw!r}, auto_clips={auto_clips}, num_clips={num_clips}")
            clip_duration = int(settings.get("clip_duration", CLIP_DURATION))
            min_gap = int(settings.get("min_gap", MIN_GAP))
            style = settings.get("subtitle_style", SUBTITLE_STYLE)
            model = settings.get("whisper_model", WHISPER_MODEL)
            language = settings.get("whisper_language") or None
            preset = settings.get("ffmpeg_preset", FFMPEG_PRESET)
            crf = str(settings.get("video_crf", VIDEO_CRF))
            crop_vertical = settings.get("crop_vertical", CROP_VERTICAL)
            effect = settings.get("video_effect", "none")
            music_file = settings.get("music_file", None)
            music_volume = float(settings.get("music_volume", 0.12))
            music_start = float(settings.get("music_start", 0))
            music_end = float(settings.get("music_end", 0))

            # ── 1. Download ──────────────────────────────────────────
            if self._cancel:
                return self._cancelled()
            self._push("download", 0, "Downloading video...")

            video_path = self._download_with_progress(url)

            self._push("download", 100, f"Downloaded: {video_path.name}")

            # ── Get video duration (needed for auto clip count + sentence snapping) ──
            try:
                from subprocess_utils import run as _srun
                _r = _srun(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(video_path)],
                    capture_output=True, text=True, timeout=10,
                )
                vid_duration = float(_r.stdout.strip())
            except Exception:
                vid_duration = 600  # default 10 min

            # ── Auto clip count ──────────────────────────────────────
            if auto_clips:
                vid_w, vid_h = get_dimensions(video_path)
                # Smart auto: scale clips based on video length
                #   < 5 min  → 2-3 clips
                #   5-15 min → 3-5 clips
                #   15-30 min → 5-8 clips
                #   30-60 min → 8-15 clips
                #   1-2 hrs  → 15-25 clips
                #   2+ hrs   → 25-40 clips
                # Formula: roughly 1 clip per 3-4 minutes, with a minimum of 2
                vid_mins = vid_duration / 60
                if vid_mins < 5:
                    num_clips = max(2, min(3, int(vid_mins / 1.5)))
                elif vid_mins < 15:
                    num_clips = max(3, int(vid_mins / 3))
                elif vid_mins < 30:
                    num_clips = max(5, int(vid_mins / 3.5))
                elif vid_mins < 60:
                    num_clips = max(8, int(vid_mins / 3.5))
                elif vid_mins < 120:
                    num_clips = max(15, min(30, int(vid_mins / 4)))
                else:
                    num_clips = max(25, min(50, int(vid_mins / 4)))
                # Also consider clip duration — shorter clips = can fit more
                if clip_duration < 20:
                    num_clips = int(num_clips * 1.3)
                elif clip_duration > 60:
                    num_clips = max(2, int(num_clips * 0.7))
                num_clips = max(2, min(50, num_clips))
                self._push("detect", 0, f"Auto: {num_clips} clips for {int(vid_mins)}min video")
                print(f"[+] Auto clip count: {num_clips} (video is {vid_duration:.0f}s / {vid_mins:.1f}min)")

            # ── 2. Detect viral moments ──────────────────────────────
            if self._cancel:
                return self._cancelled()
            self._push("detect", 0, "Analyzing video for viral moments...")

            moments = find_viral_moments(
                video_path, num_clips=num_clips, clip_duration=clip_duration, min_gap=min_gap
            )
            # Append moments (batch mode: preserve previous video's moments)
            self._moments.extend(moments)

            if not moments:
                self._push("detect", 100, "No moments found")
                return self._error("No viral moments found. Try a longer video or fewer clips.")

            self._push("detect", 100, f"Found {len(moments)} moments")
            self._js(f"window.onMomentsDetected({json.dumps(moments)})")

            # ── 3. Process each clip ─────────────────────────────────
            stem = video_path.stem[:50]
            done: list[Path] = []
            total = len(moments)

            # Buffer to extend audio extraction for sentence boundary search
            SENTENCE_BUFFER = 5  # seconds beyond original end

            for idx, m in enumerate(moments):
                if self._cancel:
                    return self._cancelled()
                clip_num = idx + 1
                start, end = m["start"], m["end"]
                original_duration = end - start

                # ── 3b: extract audio WITH extended buffer ──
                # Extract a few extra seconds beyond the clip end so we can
                # find a natural sentence ending even if it falls just outside.
                self._clip_push(clip_num, total, "audio", 50, f"Clip {clip_num}/{total}: Extracting audio...")
                wav = SUBTITLES_DIR / f"{stem}_c{clip_num}.wav"
                extended_end = min(end + SENTENCE_BUFFER, int(vid_duration))
                r = extract_audio_clip(video_path, start, extended_end, wav)
                if not r:
                    self._clip_push(clip_num, total, "render", 100, f"Clip {clip_num}: failed, skipping")
                    continue
                self._clip_push(clip_num, total, "audio", 100, "Audio extracted")

                # ── 3c: transcribe the extended audio ──
                if self._cancel:
                    return self._cancelled()
                self._clip_push(clip_num, total, "transcribe", 0, f"Clip {clip_num}/{total}: Transcribing...")
                words = transcribe_clip(wav, model_size=model, language=language)
                self._clip_push(clip_num, total, "transcribe", 100, f"{len(words)} words transcribed")

                # ── 3c.1: find natural sentence boundary ──
                # Adjust clip end so the speaker finishes their sentence
                new_duration = find_sentence_boundary(
                    words,
                    clip_duration=float(original_duration),
                    min_keep=0.60,
                    max_extend=float(SENTENCE_BUFFER),
                )
                if new_duration is not None:
                    end = start + int(new_duration + 0.5)  # round to nearest second
                    # Trim words to the adjusted duration
                    words = [w for w in words if w["end"] <= new_duration + 0.1]
                    self._clip_push(clip_num, total, "transcribe", 100,
                                    f"Adjusted to {end - start}s (sentence end)")
                else:
                    # No boundary found — trim words to original duration
                    words = [w for w in words if w["end"] <= original_duration + 0.1]

                # Update moment info for UI
                m["end"] = end
                m["duration"] = end - start
                # Store transcript text for LLM title generation
                m["transcript"] = " ".join(w.get("word", w.get("text", "")) for w in words).strip()

                # ── 3a: compute crop params (uses adjusted start/end) ──
                crop_params = None
                crop_w, crop_h = get_dimensions(video_path)
                if crop_vertical:
                    if self._cancel:
                        return self._cancelled()
                    self._clip_push(clip_num, total, "audio", 0, f"Clip {clip_num}/{total}: Tracking speakers...")
                    try:
                        crop_params = get_crop_params_dynamic(video_path, start, end)
                    except Exception as e:
                        print(f"[!] Crop detection failed for clip {clip_num}: {e}")
                        crop_params = None
                    if crop_params:
                        crop_w, crop_h = crop_params[0], crop_params[1]

                # ── 3d: subtitles (pass cropped dimensions) ──
                if self._cancel:
                    return self._cancelled()
                self._clip_push(clip_num, total, "subtitle", 0, f"Clip {clip_num}/{total}: Generating subtitles...")
                ass = SUBTITLES_DIR / f"{stem}_c{clip_num}.ass"
                generate_subtitles(
                    words, ass,
                    video_width=crop_w,
                    video_height=crop_h,
                    style=style,
                )
                self._clip_push(clip_num, total, "subtitle", 100, "Subtitles generated")

                # ── 3e: render clip with crop + burned subs ──
                if self._cancel:
                    return self._cancelled()
                self._clip_push(clip_num, total, "render", 0, f"Clip {clip_num}/{total}: Rendering...")
                out = CLIPS_DIR / f"{stem}_viral{clip_num}.mp4"
                clip_result = extract_clip(
                    video_path, start, end, out,
                    subtitle_path=ass if words else None,
                    crop_params=crop_params,
                    preset=preset, crf=crf,
                )
                if clip_result and clip_result.path:
                    # Post-processing: apply video effect
                    if effect and effect != "none":
                        self._clip_push(clip_num, total, "render", 80,
                                        f"Clip {clip_num}/{total}: Applying {effect} effect...")
                        apply_video_effect(clip_result.path, effect, preset, crf)

                    # Post-processing: mix background music
                    if music_file:
                        music_path = Path(music_file)
                        if not music_path.is_absolute():
                            music_path = MUSIC_DIR / music_file
                        if music_path.exists():
                            self._clip_push(clip_num, total, "render", 90,
                                            f"Clip {clip_num}/{total}: Adding music...")
                            add_background_music(
                                clip_result.path, music_path, music_volume,
                                trim_start=music_start, trim_end=music_end,
                            )

                    done.append(clip_result.path)
                    if not clip_result.subtitles_burned and clip_result.warning:
                        self._clip_push(clip_num, total, "render", 100,
                                        f"Clip {clip_num} done (WARNING: {clip_result.warning})")
                    else:
                        self._clip_push(clip_num, total, "render", 100, f"Clip {clip_num} complete!")
                elif clip_result and not clip_result.path:
                    self._clip_push(clip_num, total, "render", 100, f"Clip {clip_num} failed")
                else:
                    self._clip_push(clip_num, total, "render", 100, f"Clip {clip_num} failed")

                # cleanup temp wav
                try:
                    wav.unlink(missing_ok=True)
                except Exception:
                    pass

            # Append results (batch mode: preserve previous video's clips)
            self._results.extend(done)
            self._save_state()
            self._js(f"window.onPipelineComplete(true, {len(done)}, {total}, null)")

        except CancelledError:
            return self._cancelled()
        except Exception as e:
            self._error(str(e))
        finally:
            self._processing = False

    # ── Download with real progress ──────────────────────────────────────

    def _download_with_progress(self, url):
        """Download via yt-dlp with progress_hooks for live percent updates."""

        def hook(d):
            if self._cancel:
                raise CancelledError("Download cancelled")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = int(downloaded / total * 100)
                    self._push("download", pct, f"Downloading... {pct}%")
            elif d["status"] == "finished":
                self._push("download", 95, "Merging formats...")

        DOWNLOADS_DIR.mkdir(exist_ok=True)

        # Prefer H.264 (avc1) — universally supported by ffmpeg.
        # restrictfilenames removes unicode chars that break Windows paths.
        fmt = (
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
            "bestvideo[height<=1080]+bestaudio/"
            "best"
        )
        ydl_opts = {
            "format": fmt,
            "outtmpl": str(DOWNLOADS_DIR / "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
        }

        # If it looks like a local file path, just use it directly
        if Path(url).exists():
            return Path(url)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return Path(ydl.prepare_filename(info))

    # ── Upload orchestrator (background thread) ──────────────────────────

    def _run_upload(self, clips_metadata, schedule_start_iso, interval_hours, channel_id=None):
        try:
            start_time = datetime.fromisoformat(schedule_start_iso) if schedule_start_iso else None
            total = len(clips_metadata)

            for i, meta in enumerate(clips_metadata):
                if self._cancel:
                    return self._cancelled()
                pct = int((i / total) * 100)
                self._push("upload", pct, f"Uploading clip {i + 1}/{total}...")

                idx = meta.get("index", i)
                if idx >= len(self._results):
                    continue
                video_path = self._results[idx]

                scheduled = None
                if start_time:
                    scheduled = start_time + timedelta(hours=int(interval_hours) * i)

                upload_to_youtube(
                    video_path,
                    title=meta.get("title", f"Viral Clip #{i + 1}"),
                    description=meta.get("description", ""),
                    tags=meta.get("tags", ["shorts", "viral", "clips"]),
                    category_id=str(meta.get("category_id", "22")),
                    privacy=meta.get("privacy", "private"),
                    scheduled_time=scheduled,
                    channel_id=channel_id,
                )

                # Auto-delete from disk after successful upload
                if self._delete_after_upload:
                    self._delete_uploaded_clip(idx, video_path)

            self._push("upload", 100, f"All {total} clips uploaded!")
            self._js(f"window.onPipelineComplete(true, {total}, {total}, null)")

        except Exception as e:
            self._error(f"Upload failed: {e}")
        finally:
            self._processing = False

    # ── Background upload scheduler ──────────────────────────────────────

    def _scheduler_loop(self):
        """Check every 30s for scheduled uploads whose time has arrived."""
        while self._scheduler_running:
            now = datetime.now()
            changed = False

            for item in self._scheduled:
                if item.get("uploaded"):
                    continue
                try:
                    sched_dt = datetime.fromisoformat(f"{item['date']}T{item['time']}")
                except (KeyError, ValueError):
                    continue

                if now >= sched_dt:
                    clip_idx = item.get("clipIdx", -1)
                    if 0 <= clip_idx < len(self._results):
                        video_path = self._results[clip_idx]
                        if video_path.exists():
                            title = item.get("title", f"Viral Clip #{clip_idx + 1}")
                            print(f"[scheduler] Uploading Clip {clip_idx + 1}: {title}")
                            self._js(f"window.onSchedulerStatus('Uploading: {self._esc(title)}')")
                            try:
                                tags = item.get("tags", "shorts, viral, clips")
                                if isinstance(tags, str):
                                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                                upload_to_youtube(
                                    video_path,
                                    title=title,
                                    description=item.get("description", ""),
                                    tags=tags,
                                    category_id=str(item.get("category_id", "22")),
                                    privacy=item.get("privacy", "private"),
                                    channel_id=item.get("channel_id"),
                                )
                                item["uploaded"] = True
                                changed = True
                                print(f"[scheduler] Uploaded: {title}")
                                self._js(f"window.onScheduledUploadDone({clip_idx}, true, null)")

                                # Auto-delete from disk after successful upload
                                if self._delete_after_upload:
                                    self._delete_uploaded_clip(clip_idx, video_path)

                            except Exception as e:
                                print(f"[scheduler] Upload failed: {e}")
                                self._js(f"window.onScheduledUploadDone({clip_idx}, false, `{self._esc(str(e))}`)")

            if changed:
                self._save_state()
                self._js("window.onScheduleUpdated()")

            time.sleep(30)

    def _delete_uploaded_clip(self, clip_idx, video_path):
        """Delete a clip file from disk after successful upload."""
        try:
            if video_path.exists():
                video_path.unlink()
                print(f"[cleanup] Deleted uploaded clip: {video_path.name}")
                self._js(f"window.onClipDeleted({clip_idx}, `{self._esc(video_path.name)}`)")
        except Exception as e:
            print(f"[cleanup] Failed to delete {video_path.name}: {e}")

    # ── State persistence ────────────────────────────────────────────────

    def _save_state(self):
        """Persist results, moments, schedule, and settings to JSON."""
        data = {
            "results": [str(p) for p in self._results],
            "moments": self._moments,
            "scheduled": self._scheduled,
            "delete_after_upload": self._delete_after_upload,
            "user_settings": self._user_settings,
        }
        try:
            STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[!] Failed to save state: {e}")

    def _load_state(self):
        """Load persisted state from previous session."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            # Restore results as Path objects, keeping moments aligned
            paths = [Path(p) for p in data.get("results", [])]
            all_moments = data.get("moments", [])
            # Filter out missing files AND their corresponding moments
            self._results = []
            self._moments = []
            for i, p in enumerate(paths):
                if p.exists():
                    self._results.append(p)
                    self._moments.append(all_moments[i] if i < len(all_moments) else {})
            self._scheduled = data.get("scheduled", [])
            self._delete_after_upload = data.get("delete_after_upload", False)
            self._user_settings = data.get("user_settings", {})
            print(f"[+] Restored state: {len(self._results)} clips, {len(self._scheduled)} scheduled")
            if self._user_settings:
                print(f"[+] Restored user settings: {list(self._user_settings.keys())}")
        except Exception as e:
            print(f"[!] Failed to load state: {e}")

    # ── Progress push helpers ────────────────────────────────────────────

    def _push(self, stage, pct, msg):
        self._js(f"window.onPipelineProgress('{stage}', {pct}, `{self._esc(msg)}`)")

    def _clip_push(self, num, total, substep, pct, msg):
        self._js(
            f"window.onClipProgress({num}, {total}, '{substep}', {pct}, `{self._esc(msg)}`)"
        )

    def _error(self, msg):
        self._js(f"window.onPipelineComplete(false, 0, 0, `{self._esc(msg)}`)")
        self._processing = False

    def _cancelled(self):
        self._js("window.onPipelineCancelled()")
        self._processing = False

    def _js(self, code):
        """Execute JS in the frontend. Queues calls if window is hidden/minimized."""
        try:
            if self._window:
                self._window.evaluate_js(code)
                return
        except Exception:
            pass
        # Window is hidden or unavailable — queue for when it comes back.
        # Only keep the last progress update per type (avoid flooding the queue)
        # but ALWAYS keep completion/error/cancel callbacks.
        is_progress = "onPipelineProgress" in code or "onClipProgress" in code
        is_console = "onConsoleLog" in code
        if is_progress:
            # Replace previous progress of same type
            self._pending_js = [c for c in self._pending_js
                                if ("onPipelineProgress" not in c and "onClipProgress" not in c)]
        if is_console and len([c for c in self._pending_js if "onConsoleLog" in c]) > 200:
            # Trim old console logs to avoid memory bloat
            non_console = [c for c in self._pending_js if "onConsoleLog" not in c]
            console = [c for c in self._pending_js if "onConsoleLog" in c][-100:]
            self._pending_js = non_console + console
        self._pending_js.append(code)

    def flush_pending_js(self):
        """Called from frontend when window is restored — replay any queued JS calls."""
        pending = list(self._pending_js)
        self._pending_js.clear()
        for code in pending:
            try:
                if self._window:
                    self._window.evaluate_js(code)
            except Exception:
                pass
        return {"flushed": len(pending)}

    @staticmethod
    def _esc(s):
        return str(s).replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
