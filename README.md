<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/FFmpeg-required-007808?style=for-the-badge&logo=ffmpeg&logoColor=white" alt="FFmpeg">
  <img src="https://img.shields.io/badge/Ollama-optional-FF6B6B?style=for-the-badge" alt="Ollama">
  <img src="https://img.shields.io/badge/license-MIT-blue?style=for-the-badge" alt="License">
  <img src="https://img.shields.io/badge/platform-Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Windows">
</p>

<h1 align="center">
  <br>
  ViriaRevive
  <br>
  <sub><sup>AI-Powered Viral Clip Generator</sup></sub>
</h1>

<p align="center">
  <strong>Turn long-form YouTube videos into viral short-form clips — automatically.</strong>
  <br>
  Detect the best moments, crop to 9:16, add stylish subtitles, generate AI titles, and schedule uploads to YouTube — all from one app.
</p>

<br>

<p align="center">
  <img src="docs/preview.png" alt="ViriaRevive Preview" width="850">
</p>

---

## What is ViriaRevive?

ViriaRevive is a desktop application that automates the entire pipeline of creating viral short-form content from long YouTube videos. Paste a URL, and it handles everything: downloading, detecting the most engaging moments using audio/scene analysis, cropping to vertical format with smart face tracking, burning in stylish animated subtitles, and uploading directly to your YouTube channels on a schedule.

No cloud services needed — everything runs locally on your machine.

---

## Features

### Clip Generation
- **Smart Moment Detection** — Finds the most viral-worthy segments using audio energy analysis + scene change detection
- **Vertical Auto-Crop (9:16)** — YOLO-powered person detection keeps subjects perfectly centered
- **Batch Processing** — Generate 3-10 clips per video, configurable duration (15-60s)
- **Multi-URL Queue** — Process multiple videos in one batch

### Subtitles & Styling
- **Word-by-Word Highlighting** — Animated subtitle burn-in with precise timestamps
- **3 Built-in Styles** — TikTok, Clean, and Bold
- **Whisper Transcription** — Accurate speech-to-text powered by Faster-Whisper

### AI Title Generation
- **LLM-Powered Titles** — Uses local Ollama models to generate catchy YouTube Shorts titles
- **Per-Folder Generation** — Generate titles for a specific batch of clips, not everything at once
- **Smart Fallback** — Works without Ollama using keyword extraction heuristics

### YouTube Integration
- **Multi-Account Support** — Connect and manage multiple YouTube channels
- **Smart Scheduling** — Auto-assign clips to peak upload times for maximum reach
- **Per-Folder Channel Assignment** — Choose which channel each batch of clips goes to
- **Calendar View** — Visual drag-and-drop scheduling with daily/monthly overview
- **Full Metadata Control** — Title, description, tags, category, and privacy per clip

### Audio & Effects
- **Background Music** — Browse and overlay music from a local library
- **Waveform Trimmer** — Visual audio trimming with volume control
- **Video Effects** — Pre-built effect presets for clips

### Desktop Experience
- **Modern Dark UI** — Glassmorphism design with smooth animations
- **System Tray** — Minimize to tray, auto-start with Windows
- **Live Console** — Built-in log viewer for debugging
- **Drag & Drop** — Import videos, schedule clips on calendar

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.8+ |
| Frontend | HTML5 / CSS3 / Vanilla JS |
| Desktop Shell | [pywebview](https://pywebview.flowrl.com/) |
| Video Processing | [FFmpeg](https://ffmpeg.org/) |
| Video Download | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| Speech-to-Text | [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) |
| Person Detection | [YOLOv8](https://github.com/ultralytics/ultralytics) + OpenCV |
| AI Titles | [Ollama](https://ollama.ai/) (local LLM) |
| YouTube API | Google API v3 with OAuth 2.0 |

---

## Getting Started

### Prerequisites

1. **Python 3.8+** — [Download](https://www.python.org/downloads/)
2. **FFmpeg** — Must be in your system PATH
   - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html), extract, and add the `bin` folder to PATH
3. **Ollama** *(optional, for AI titles)* — [Download](https://ollama.ai/)

### Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/ViriaRevive.git
cd ViriaRevive

# Create virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

### YouTube Upload Setup

To enable uploading to YouTube, you need Google OAuth credentials:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable the **YouTube Data API v3**
4. Go to **Credentials** > **Create Credentials** > **OAuth 2.0 Client ID**
5. Select **Desktop app** as the application type
6. Download the JSON file and save it as `client_secrets.json` in the project root

> See `client_secrets.example.json` for the expected format.

### Ollama Setup *(Optional)*

For AI-powered title generation:

```bash
# Install Ollama, then:
ollama pull qwen2.5:3b
```

The app will automatically detect Ollama and use it. Without it, titles are generated using keyword extraction.

### Launch

```bash
# With console (debug mode)
python app.py

# Without console (production)
pythonw app.pyw
```

---

## Usage

### Quick Start

1. **Generate** — Paste a YouTube URL and click "Find Viral Moments"
2. **Review** — Browse detected clips in the Results tab
3. **Schedule** — Assign clips to your YouTube channel(s) via the calendar
4. **Upload** — Hit "Upload Scheduled Clips" and let it run

### CLI Mode

```bash
# Basic usage
python main.py "https://youtube.com/watch?v=VIDEO_ID"

# With options
python main.py "URL" --clips 5 --duration 30 --style bold

# Generate + upload
python main.py "URL" --upload --schedule 24
```

---

## Project Structure

```
ViriaRevive/
├── gui/
│   ├── index.html          # Main UI layout
│   ├── app.js              # Frontend logic & state
│   └── style.css           # Dark theme styles
├── app.py                  # GUI launcher (debug)
├── app.pyw                 # GUI launcher (no console)
├── main.py                 # CLI entry point
├── api_bridge.py           # Python <-> JS bridge
├── detector.py             # Viral moment detection
├── clipper.py              # FFmpeg clip extraction
├── cropper.py              # YOLO face detection + 9:16 crop
├── transcriber.py          # Faster-Whisper integration
├── subtitler.py            # ASS subtitle generation
├── title_generator.py      # Ollama / heuristic titles
├── uploader.py             # YouTube OAuth + upload
├── downloader.py           # yt-dlp wrapper
├── config.py               # Paths & configuration
├── tray.py                 # System tray integration
└── requirements.txt
```

---

## Contributing

Contributions are welcome! Feel free to open issues or submit pull requests.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<p align="center">
  <sub>Built with Python, FFmpeg, and a lot of caffeine.</sub>
</p>
