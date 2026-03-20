"""System tray integration for ViriaRevive.

Minimizes to tray instead of closing. Tray icon shows status and provides
quick access to restore, open output folder, or quit.
"""

import sys
import threading
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

import pystray


def _get_base():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent

_BASE = _get_base()
_ICON_PATH = _BASE / "gui" / "tray_icon.png"


def _create_icon_image():
    """Generate a simple 64x64 tray icon (purple/cyan gradient V shape)."""
    if _ICON_PATH.exists():
        return Image.open(str(_ICON_PATH))

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle
    draw.ellipse([2, 2, size - 3, size - 3], fill=(108, 92, 231, 255))

    # "V" letter in white
    pts = [(16, 18), (32, 50), (48, 18)]
    draw.line(pts[:2], fill=(255, 255, 255, 255), width=5)
    draw.line(pts[1:], fill=(0, 206, 201, 255), width=5)

    # Don't save to disk in frozen builds (temp dir gets cleaned up)
    if not getattr(sys, 'frozen', False):
        try:
            img.save(str(_ICON_PATH))
        except Exception:
            pass
    return img


class TrayManager:
    """Manages the system tray icon and menu."""

    def __init__(self, window, on_quit_callback=None):
        self._window = window
        self._on_quit = on_quit_callback
        self._icon = None
        self._visible = True  # tracks window visibility

    def start(self):
        """Start the tray icon in a background thread."""
        image = _create_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("Show ViriaRevive", self._show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon("ViriaRevive", image, "ViriaRevive", menu)
        threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self):
        """Stop the tray icon."""
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    def update_tooltip(self, text: str):
        """Update the tray icon tooltip text."""
        if self._icon:
            self._icon.title = text

    def on_minimize(self):
        """Called when the window is minimized — hide to tray."""
        if self._window:
            try:
                self._window.hide()
                self._visible = False
            except Exception:
                pass

    def _show_window(self, icon=None, item=None):
        """Restore the window from tray."""
        if self._window:
            try:
                self._window.show()
                self._window.restore()
                self._visible = True
            except Exception:
                pass

    def _quit(self, icon=None, item=None):
        """Quit the application entirely."""
        self.stop()
        if self._on_quit:
            self._on_quit()
        elif self._window:
            try:
                self._window.destroy()
            except Exception:
                pass
