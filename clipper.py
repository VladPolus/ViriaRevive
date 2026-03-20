import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from subprocess_utils import run as _run


@dataclass
class ClipResult:
    path: Path | None
    subtitles_burned: bool = True
    warning: str | None = None


# ── Subtitle filter detection (cached) ────────────────────────────────────────

_sub_filter_cache: str | None = None


def _detect_subtitle_filter() -> str:
    """Detect the best available subtitle filter in ffmpeg.

    Prefers 'subtitles' (better font handling on Windows) over 'ass'.
    """
    global _sub_filter_cache
    if _sub_filter_cache is not None:
        return _sub_filter_cache

    try:
        r = _run(
            ["ffmpeg", "-filters"], capture_output=True, text=True, errors="replace", timeout=10,
        )
        output = r.stdout
        for filt in ["subtitles", "ass"]:
            if re.search(rf'\b{filt}\b', output):
                _sub_filter_cache = filt
                print(f"[+] Using ffmpeg subtitle filter: {filt}")
                return filt
    except Exception:
        pass

    _sub_filter_cache = ""
    print("[!] No subtitle filter available in ffmpeg (need libass)")
    return ""


def _escape_sub_path_win(path: Path) -> str:
    """Escape a subtitle file path for ffmpeg filter on Windows."""
    s = str(path).replace("\\", "/")
    s = s.replace(":", "\\:")
    return s


def _copy_fonts_to_dir(dest_dir: Path):
    """Copy common fonts to subtitle temp dir so libass can find them without fontconfig."""
    import platform
    if platform.system() != "Windows":
        return
    fonts_dir = Path("C:/Windows/Fonts")
    for name in ["arial.ttf", "arialbd.ttf", "ariblk.ttf", "impact.ttf", "verdana.ttf"]:
        src = fonts_dir / name
        dst = dest_dir / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(str(src), str(dst))
            except OSError:
                pass


def _fonts_dir_option(sub_dir: Path, use_cwd: bool) -> str:
    """Return fontsdir option for subtitle filter. Uses local dir with copied fonts."""
    import platform
    if platform.system() != "Windows":
        return ""
    if use_cwd:
        return ":fontsdir=."
    escaped = str(sub_dir).replace("\\", "/").replace(":", "\\:")
    return f":fontsdir={escaped}"


def _prepare_subtitle_file(subtitle_path: Path, output_stem: str) -> tuple[Path | None, Path | None]:
    """Copy subtitle file to a temp location with a safe ASCII name.

    Returns (temp_sub_path, temp_dir) or (None, None).
    """
    if not subtitle_path or not Path(subtitle_path).exists():
        return None, None
    if Path(subtitle_path).stat().st_size <= 50:
        return None, None

    sub_dir = Path(tempfile.gettempdir()) / "viria_subs"
    sub_dir.mkdir(exist_ok=True)
    temp_sub = sub_dir / f"sub_{output_stem}.ass"

    # Plain copy — no BOM (BOM breaks libass ASS header parsing)
    shutil.copy2(str(subtitle_path), str(temp_sub))

    # Copy font files locally so libass finds them without fontconfig
    _copy_fonts_to_dir(sub_dir)

    return temp_sub, sub_dir


def _try_subtitle_burn(input_path: Path, output_path: Path, temp_sub: Path, sub_dir: Path,
                        preset: str, crf: str, copy_audio: bool = False) -> bool:
    """Try to burn subtitles into a video. Tries multiple approaches.

    Returns True on success.
    """
    filt = _detect_subtitle_filter()
    if not filt:
        return False

    audio_args = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-strict", "-2", "-b:a", "128k"]

    # Attempt 1: filename-only with CWD set to subtitle directory + local fontsdir
    fontsdir_cwd = _fonts_dir_option(sub_dir, use_cwd=True)
    vf = f"{filt}={temp_sub.name}{fontsdir_cwd}"
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
        *audio_args,
        str(output_path),
    ]
    print(f"    Subs attempt 1 (cwd): {' '.join(cmd)}")
    r = _run(cmd, capture_output=True, text=True, errors="replace", cwd=str(sub_dir))
    if r.returncode == 0:
        if r.stderr:
            # Log stderr to catch font warnings
            stderr_lines = [l for l in r.stderr.split('\n') if 'font' in l.lower() or 'libass' in l.lower()]
            if stderr_lines:
                print(f"    [i] font info: {'; '.join(stderr_lines[:3])}")
        print(f"    [+] Subtitles burned successfully (cwd method)")
        return True

    print(f"    [!] Attempt 1 failed: {r.stderr[-200:]}")

    # Attempt 2: full escaped path + fontsdir, no CWD
    escaped = _escape_sub_path_win(temp_sub)
    fontsdir_full = _fonts_dir_option(sub_dir, use_cwd=False)
    vf2 = f"{filt}={escaped}{fontsdir_full}"
    cmd2 = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", vf2,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
        *audio_args,
        str(output_path),
    ]
    print(f"    Subs attempt 2 (escaped path): {' '.join(cmd2)}")
    r2 = _run(cmd2, capture_output=True, text=True, errors="replace")
    if r2.returncode == 0:
        if r2.stderr:
            stderr_lines = [l for l in r2.stderr.split('\n') if 'font' in l.lower() or 'libass' in l.lower()]
            if stderr_lines:
                print(f"    [i] font info: {'; '.join(stderr_lines[:3])}")
        print(f"    [+] Subtitles burned successfully (escaped path method)")
        return True

    print(f"    [!] Attempt 2 failed: {r2.stderr[-200:]}")

    # Attempt 3: try the other filter if available
    other = "ass" if filt == "subtitles" else "subtitles"
    try:
        r_check = _run(["ffmpeg", "-filters"], capture_output=True, text=True, timeout=10)
        if re.search(rf'\b{other}\b', r_check.stdout):
            vf3 = f"{other}={temp_sub.name}{fontsdir_cwd}"
            cmd3 = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-vf", vf3,
                "-c:v", "libx264", "-preset", preset, "-crf", crf,
                "-pix_fmt", "yuv420p",
                *audio_args,
                str(output_path),
            ]
            print(f"    Subs attempt 3 ({other} filter): {' '.join(cmd3)}")
            r3 = _run(cmd3, capture_output=True, text=True, errors="replace", cwd=str(sub_dir))
            if r3.returncode == 0:
                if r3.stderr:
                    stderr_lines = [l for l in r3.stderr.split('\n') if 'font' in l.lower() or 'libass' in l.lower()]
                    if stderr_lines:
                        print(f"    [i] font info: {'; '.join(stderr_lines[:3])}")
                print(f"    [+] Subtitles burned successfully ({other} filter)")
                return True
            print(f"    [!] Attempt 3 failed: {r3.stderr[-200:]}")
    except Exception:
        pass

    return False


# ── Crop filter expression builder ───────────────────────────────────────────


def _build_crop_vf(crop_params: tuple, duration: float) -> str:
    """Build the -vf crop filter string. Handles static and dynamic crop.

    For dynamic crop, builds a piecewise-linear time expression for the
    x/y offset — no external files needed, works on all ffmpeg versions.
    """
    if len(crop_params) == 4:
        # Static crop: (cw, ch, cx, cy)
        cw, ch, cx, cy = crop_params
        return f"crop={cw}:{ch}:{cx}:{cy}"

    if len(crop_params) == 3 and isinstance(crop_params[2], list):
        # Dynamic crop: (cw, ch, [(t, x, y), ...])
        cw, ch, keyframes = crop_params
        if not keyframes:
            return f"crop={cw}:{ch}:0:0"

        # Downsample keyframes to max 15 to keep expression manageable.
        # IMPORTANT: Always keep keyframes where position changes (transitions).
        # Only drop keyframes that repeat the same position as their predecessor.
        if len(keyframes) > 15:
            # First pass: mark all transition keyframes (position changes)
            must_keep = {0, len(keyframes) - 1}  # always keep first and last
            for i in range(1, len(keyframes)):
                prev_x, prev_y = keyframes[i - 1][1], keyframes[i - 1][2]
                cur_x, cur_y = keyframes[i][1], keyframes[i][2]
                if cur_x != prev_x or cur_y != prev_y:
                    must_keep.add(i)
                    if i > 0:
                        must_keep.add(i - 1)  # keep the frame before transition too

            if len(must_keep) <= 15:
                # We can fit all transitions — fill remaining slots evenly
                remaining = 15 - len(must_keep)
                optional = [i for i in range(len(keyframes)) if i not in must_keep]
                if optional and remaining > 0:
                    step = max(1, len(optional) / remaining)
                    extras = {optional[int(j * step)] for j in range(min(remaining, len(optional)))}
                    must_keep |= extras
                keyframes = [keyframes[i] for i in sorted(must_keep)]
            else:
                # More than 15 transitions — keep them all, they're all important
                keyframes = [keyframes[i] for i in sorted(must_keep)]

        # Build step-function x and y expressions
        x_expr = _build_lerp_expr([t for t, x, y in keyframes], [x for t, x, y in keyframes])
        y_expr = _build_lerp_expr([t for t, x, y in keyframes], [y for t, x, y in keyframes])

        return f"crop={cw}:{ch}:{x_expr}:{y_expr}"

    # Fallback — shouldn't happen
    cw, ch = crop_params[0], crop_params[1]
    return f"crop={cw}:{ch}:0:0"


def _build_lerp_expr(times: list, values: list) -> str:
    """Build an ffmpeg step-function expression from keyframes (instant cuts).

    For 3 keyframes at t=0,4,8 with values 100,200,150:
    → if(lt(t,4),100,if(lt(t,8),200,150))
    """
    if not times or not values:
        return "0"
    if len(set(values)) == 1:
        return str(int(values[0]))
    if len(times) == 1:
        return str(int(values[0]))
    return _step_recursive(times, values, 0)


def _step_recursive(times, values, idx):
    """Recursively build nested if() for step function (instant cuts)."""
    if idx >= len(times) - 1:
        return str(int(values[-1]))

    t1 = times[idx + 1]
    v0 = int(values[idx])
    rest = _step_recursive(times, values, idx + 1)

    if v0 == int(values[idx + 1]) and idx + 2 >= len(times):
        return str(v0)

    return f"if(lt(t\\,{t1:.3f})\\,{v0}\\,{rest})"


# ── Main extract function ────────────────────────────────────────────────────


def extract_clip(
    video_path: Path,
    start: int,
    end: int,
    output_path: Path,
    subtitle_path: Path = None,
    crop_params: tuple = None,
    preset: str = "ultrafast",
    crf: str = "23",
) -> ClipResult:
    """Extract a clip, optionally cropping to 9:16 and burning subtitles.

    Uses a TWO-PASS approach when both crop and subtitles are needed:
      Pass 1 → crop the video (fast, near-lossless)
      Pass 2 → burn subtitles onto the cropped video

    crop_params can be:
      - (cw, ch, cx, cy)         → static crop (4-tuple)
      - (cw, ch, keyframes_list) → dynamic crop (3-tuple)
    """
    import shutil

    duration = end - start

    video_path = Path(video_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Prepare subtitle temp copy
    temp_sub, sub_dir = _prepare_subtitle_file(subtitle_path, output_path.stem)

    print(f"[*] Clipping {_fmt(start)} -> {_fmt(end)}  ({duration}s)")

    # ── CASE A: crop + subtitles → two-pass ──────────────────────────────
    if crop_params and temp_sub:
        # Pass 1: crop only → temp file
        temp_cropped = output_path.with_name(output_path.stem + "_tmp_crop.mp4")
        crop_vf = _build_crop_vf(crop_params, duration)

        cmd1 = [
            "ffmpeg", "-y", "-ss", str(start), "-i", str(video_path), "-t", str(duration),
            "-vf", crop_vf,
            "-c:v", "libx264", "-preset", preset, "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-strict", "-2", "-b:a", "192k",
            str(temp_cropped),
        ]
        print(f"    Pass 1 (crop): {' '.join(cmd1)}")
        r1 = _run(cmd1, capture_output=True, text=True, errors="replace")

        if r1.returncode != 0:
            print(f"[!] Pass 1 crop failed:\n{r1.stderr[-500:]}")
            _cleanup(temp_cropped)
            _cleanup(temp_sub)
            result = _fallback_stream_copy(video_path, start, duration, output_path)
            return ClipResult(path=result, subtitles_burned=False, warning="Crop failed")

        # Pass 2: burn subtitles
        sub_ok = _try_subtitle_burn(temp_cropped, output_path, temp_sub, sub_dir,
                                     preset, crf, copy_audio=True)

        if sub_ok:
            _cleanup(temp_cropped)
            _cleanup(temp_sub)
            print(f"[+] Saved {output_path.name}")
            return ClipResult(path=output_path)
        else:
            # Subtitle burn failed — use cropped-only version
            _rename_safe(temp_cropped, output_path)
            _cleanup(temp_sub)
            print(f"[!] Saved (crop only, no subs): {output_path.name}")
            return ClipResult(path=output_path, subtitles_burned=False,
                              warning="Subtitle burn failed — ffmpeg may lack libass")

    # ── CASE B: crop only ────────────────────────────────────────────────
    if crop_params:
        crop_vf = _build_crop_vf(crop_params, duration)
        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-i", str(video_path), "-t", str(duration),
            "-vf", crop_vf,
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-strict", "-2", "-b:a", "128k",
            str(output_path),
        ]
        print(f"    cmd (crop): {' '.join(cmd)}")
        r = _run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(f"[!] Crop failed:\n{r.stderr[-400:]}")
            result = _fallback_stream_copy(video_path, start, duration, output_path)
            return ClipResult(path=result)
        print(f"[+] Saved {output_path.name}")
        return ClipResult(path=output_path)

    # ── CASE C: subtitles only ───────────────────────────────────────────
    if temp_sub:
        temp_input = output_path.with_name(output_path.stem + "_tmp_nosub.mp4")
        cmd_extract = [
            "ffmpeg", "-y", "-ss", str(start), "-i", str(video_path), "-t", str(duration),
            "-c:v", "libx264", "-preset", preset, "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-strict", "-2", "-b:a", "128k",
            str(temp_input),
        ]
        r_ext = _run(cmd_extract, capture_output=True, text=True, errors="replace")
        if r_ext.returncode != 0:
            _cleanup(temp_input)
            _cleanup(temp_sub)
            result = _fallback_stream_copy(video_path, start, duration, output_path)
            return ClipResult(path=result, subtitles_burned=False, warning="Extract failed")

        sub_ok = _try_subtitle_burn(temp_input, output_path, temp_sub, sub_dir,
                                     preset, crf, copy_audio=True)
        _cleanup(temp_input)
        _cleanup(temp_sub)

        if sub_ok:
            print(f"[+] Saved {output_path.name}")
            return ClipResult(path=output_path)
        else:
            result = _fallback_stream_copy(video_path, start, duration, output_path)
            return ClipResult(path=result, subtitles_burned=False,
                              warning="Subtitle filter failed — check ffmpeg libass support")

    # ── CASE D: no filters → stream copy ─────────────────────────────────
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
        "-t", str(duration), "-c", "copy", str(output_path),
    ]
    r = _run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"[!] Stream copy failed:\n{r.stderr[-400:]}")
        return ClipResult(path=None)
    print(f"[+] Saved {output_path.name}")
    return ClipResult(path=output_path)


def extract_audio_clip(video_path: Path, start: int, end: int, output_path: Path) -> Path | None:
    """Extract mono 16 kHz WAV audio for whisper transcription."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-i", str(video_path), "-t", str(end - start),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(output_path),
    ]
    r = _run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"[!] Audio extraction error:\n{r.stderr[-400:]}")
        return None
    return output_path


# ── Utility helpers ──────────────────────────────────────────────────────────


def _rename_safe(src: Path, dst: Path):
    import shutil
    try:
        if dst.exists():
            dst.unlink()
        src.rename(dst)
    except Exception:
        shutil.move(str(src), str(dst))


def _fallback_stream_copy(video_path, start, duration, output_path):
    print("[!] Falling back to stream copy...")
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", str(video_path),
        "-t", str(duration), "-c", "copy", str(output_path),
    ]
    r = _run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"[!] Stream copy also failed:\n{r.stderr[-400:]}")
        return None
    print(f"[+] Saved (stream copy): {output_path.name}")
    return output_path


def _cleanup(path):
    if path and Path(path).exists():
        try:
            Path(path).unlink()
        except OSError:
            pass


def _fmt(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── Post-processing: background music ───────────────────────────────────────


def add_background_music(
    clip_path: Path,
    music_path: Path,
    volume: float = 0.12,
    trim_start: float = 0,
    trim_end: float = 0,
) -> bool:
    """Mix background music into a clip at the given volume level.

    - music_path: path to an audio file (mp3/wav/aac)
    - volume: 0.0-1.0, default 0.12 (12% = subtle background)
    - trim_start/trim_end: use only this portion of the music file (seconds).
      If both are 0 or trim_end <= trim_start, uses the full track.
    - The trimmed selection is looped if shorter than the clip
    - The original audio is kept at full volume
    - Overwrites the clip in-place

    Returns True on success.
    """
    clip_path = Path(clip_path).resolve()
    music_path = Path(music_path).resolve()

    if not clip_path.exists() or not music_path.exists():
        print(f"[!] Music mix: missing file (clip={clip_path.exists()}, music={music_path.exists()})")
        return False

    temp_out = clip_path.with_name(clip_path.stem + "_music_tmp.mp4")

    # Get clip duration
    try:
        r = _run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(clip_path)],
            capture_output=True, text=True, timeout=10,
        )
        clip_dur = float(r.stdout.strip())
    except Exception:
        clip_dur = 60

    # Build audio filter for music input:
    # 1. If trimming, first seek + trim to the selected portion
    # 2. Loop the (trimmed) audio to fill the clip duration
    # 3. Apply volume
    has_trim = trim_end > trim_start > 0
    music_filter_parts = []

    if has_trim:
        trim_duration = trim_end - trim_start
        # atrim to extract the selected portion, then asetpts to reset timestamps
        music_filter_parts.append(
            f"[1:a]atrim=start={trim_start:.3f}:end={trim_end:.3f},asetpts=PTS-STARTPTS"
        )
        # Loop the trimmed portion to fill clip duration
        music_filter_parts.append(
            f"aloop=loop=-1:size={int(trim_duration * 48000)},"
            f"atrim=duration={clip_dur:.3f},volume={volume:.2f}[bg]"
        )
        af_music = ",".join(music_filter_parts)
    else:
        # No trim — loop the full track
        af_music = (
            f"[1:a]aloop=loop=-1:size=2e+09,"
            f"atrim=duration={clip_dur:.3f},volume={volume:.2f}[bg]"
        )

    af = f"{af_music};[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-i", str(music_path),
        "-filter_complex", af,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        str(temp_out),
    ]

    trim_info = f", trim {trim_start:.1f}-{trim_end:.1f}s" if has_trim else ""
    print(f"[*] Mixing background music ({volume:.0%} vol{trim_info})...")
    r = _run(cmd, capture_output=True, text=True, errors="replace")

    if r.returncode == 0 and temp_out.exists():
        _rename_safe(temp_out, clip_path)
        print(f"[+] Background music added to {clip_path.name}")
        return True
    else:
        print(f"[!] Music mix failed:\n{r.stderr[-400:]}")
        _cleanup(temp_out)
        return False


# ── Post-processing: video effects ──────────────────────────────────────────

# Available effects presets
EFFECTS_PRESETS = {
    "none": {
        "label": "No Effects",
        "desc": "Clean original look",
        "vf": None,
    },
    "cinematic": {
        "label": "Cinematic",
        "desc": "Slight contrast boost + warm tones",
        "vf": "eq=contrast=1.08:brightness=0.02:saturation=1.15",
    },
    "vibrant": {
        "label": "Vibrant",
        "desc": "Vivid colors + sharpness",
        "vf": "eq=saturation=1.35:contrast=1.05,unsharp=3:3:1.0",
    },
    "moody": {
        "label": "Moody",
        "desc": "Dark cinematic with crushed blacks",
        "vf": "eq=contrast=1.2:brightness=-0.03:saturation=0.85,curves=m='0/0.05 0.5/0.45 1/0.95'",
    },
    "vintage": {
        "label": "Vintage",
        "desc": "Warm retro film look",
        "vf": "eq=saturation=0.75:contrast=1.1:brightness=0.03,colorbalance=rs=0.08:gs=0.02:bs=-0.06",
    },
    "bright": {
        "label": "Bright & Clean",
        "desc": "Boosted brightness + light feel",
        "vf": "eq=brightness=0.06:contrast=1.05:saturation=1.1",
    },
    "bw": {
        "label": "Black & White",
        "desc": "Classic monochrome with contrast",
        "vf": "eq=saturation=0:contrast=1.15",
    },
}


def apply_video_effect(
    clip_path: Path,
    effect: str = "none",
    preset: str = "ultrafast",
    crf: str = "23",
) -> bool:
    """Apply a video effect preset to a clip (in-place).

    effect: key from EFFECTS_PRESETS ('cinematic', 'vibrant', etc.)
    Returns True on success.
    """
    if effect == "none" or effect not in EFFECTS_PRESETS:
        return True

    vf = EFFECTS_PRESETS[effect]["vf"]
    if not vf:
        return True

    clip_path = Path(clip_path).resolve()
    if not clip_path.exists():
        return False

    temp_out = clip_path.with_name(clip_path.stem + "_fx_tmp.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(temp_out),
    ]

    print(f"[*] Applying '{effect}' effect...")
    r = _run(cmd, capture_output=True, text=True, errors="replace")

    if r.returncode == 0 and temp_out.exists():
        _rename_safe(temp_out, clip_path)
        print(f"[+] Effect '{effect}' applied to {clip_path.name}")
        return True
    else:
        print(f"[!] Effect failed:\n{r.stderr[-400:]}")
        _cleanup(temp_out)
        return False


def get_effects_list() -> list[dict]:
    """Return list of available effects for the UI."""
    return [
        {"id": k, "label": v["label"], "desc": v["desc"]}
        for k, v in EFFECTS_PRESETS.items()
    ]
