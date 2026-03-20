import subprocess
import numpy as np
from pydub import AudioSegment
from pathlib import Path

from subprocess_utils import run as _run


def find_viral_moments(
    video_path: Path,
    num_clips: int = 5,
    clip_duration: int = 30,
    min_gap: int = 15,
) -> list:
    """Find viral moments using audio energy + scene change analysis (no AI)."""

    print("[*] Analyzing audio energy...")
    audio = AudioSegment.from_file(str(video_path))
    total_seconds = len(audio) // 1000

    if total_seconds < 10:
        print("[!] Video too short for analysis")
        return []

    # --- Audio RMS energy (1-second windows) ---
    window_ms = 1000
    energies = np.array(
        [audio[i : i + window_ms].rms for i in range(0, len(audio), window_ms)],
        dtype=float,
    )

    # Smooth
    kernel = np.ones(5) / 5
    smoothed = np.convolve(energies, kernel, mode="same")

    # --- Volume variance (dynamic = interesting) ---
    var_window = 10
    variance = np.array(
        [
            np.std(energies[max(0, i - var_window // 2) : i + var_window // 2])
            for i in range(len(energies))
        ]
    )

    # --- Scene change density ---
    print("[*] Analyzing scene changes...")
    scene_density = _scene_change_density(video_path, len(energies))

    # --- Combine (normalize each to 0-1) ---
    def norm(a):
        r = a.max() - a.min()
        return (a - a.min()) / r if r > 1e-8 else np.zeros_like(a)

    combined = (
        0.45 * norm(smoothed)
        + 0.25 * norm(variance)
        + 0.30 * norm(scene_density[: len(smoothed)])
    )

    # --- Pick top N non-overlapping peaks ---
    half = clip_duration // 2
    clips = []
    for _ in range(num_clips):
        if combined.max() <= 0:
            break
        peak = int(np.argmax(combined))
        start = max(0, peak - half)
        end = min(len(combined), start + clip_duration)
        if end - start < clip_duration and start > 0:
            start = max(0, end - clip_duration)

        clips.append(
            {"start": start, "end": end, "duration": end - start, "score": float(combined[peak])}
        )

        # mask out neighbourhood
        lo = max(0, peak - clip_duration - min_gap)
        hi = min(len(combined), peak + clip_duration + min_gap)
        combined[lo:hi] = 0

    clips.sort(key=lambda c: c["start"])

    print(f"[+] Found {len(clips)} viral moments")
    for i, c in enumerate(clips):
        print(f"    Clip {i+1}: {_fmt(c['start'])} - {_fmt(c['end'])}  (score {c['score']:.2f})")
    return clips


# ── helpers ──────────────────────────────────────────────────────────────────


def _scene_change_density(video_path: Path, length: int) -> np.ndarray:
    """Count scene changes per second using ffmpeg."""
    try:
        cmd = [
            "ffmpeg", "-i", str(video_path),
            "-vf", "fps=2,select='gt(scene,0.3)',showinfo",
            "-vsync", "vfr", "-f", "null", "-",
            "-threads", "4",
        ]
        r = _run(cmd, capture_output=True, text=True, timeout=600, errors="replace")
        timestamps = []
        for line in r.stderr.split("\n"):
            if "pts_time:" in line:
                try:
                    timestamps.append(float(line.split("pts_time:")[1].split()[0]))
                except (ValueError, IndexError):
                    pass

        density = np.zeros(length + 1)
        win = 10
        for ts in timestamps:
            lo = max(0, int(ts) - win // 2)
            hi = min(length + 1, int(ts) + win // 2)
            density[lo:hi] += 1
        return density

    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("[!] Scene detection unavailable, using audio only")
        return np.zeros(length + 1)


def _fmt(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
