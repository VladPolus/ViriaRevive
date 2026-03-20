import yt_dlp
from pathlib import Path
from config import DOWNLOADS_DIR

# Prefer H.264 (avc1) which every ffmpeg supports.
# Fallback chain avoids AV1/VP9 codec issues on Windows.
_FORMAT = (
    "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[acodec^=mp4a]/"
    "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
    "bestvideo[height<=1080]+bestaudio/"
    "best"
)


def download_video(url: str, output_dir: Path = DOWNLOADS_DIR) -> Path:
    """Download a YouTube video and return the file path."""
    output_dir.mkdir(exist_ok=True)

    ydl_opts = {
        "format": _FORMAT,
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "restrictfilenames": True,   # ASCII-safe names (no unicode quotes etc.)
        "quiet": False,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return Path(filename)
