import sys
from pathlib import Path

# In PyInstaller frozen builds, __file__ resolves to the temp _MEIPASS dir.
# User data (downloads, clips, tokens, secrets) must live next to the .exe.
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CLIPS_DIR = BASE_DIR / "clips"
SUBTITLES_DIR = BASE_DIR / "subtitles"

MUSIC_DIR = BASE_DIR / "music"

for d in [DOWNLOADS_DIR, CLIPS_DIR, SUBTITLES_DIR, MUSIC_DIR]:
    d.mkdir(exist_ok=True)

# Clip detection
NUM_CLIPS = 5
CLIP_DURATION = 30
MIN_GAP = 15

# Whisper
WHISPER_MODEL = "base"
WHISPER_LANGUAGE = None

# Subtitle style
SUBTITLE_STYLE = "tiktok"

# Cropping
CROP_VERTICAL = True          # auto-crop to 9:16 for Shorts

# FFmpeg encoding
FFMPEG_PRESET = "ultrafast"
VIDEO_CRF = "23"

# YouTube
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"
TOKEN_FILE = BASE_DIR / "token.json"
DEFAULT_TAGS = ["shorts", "viral", "clips"]
