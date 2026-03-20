#!/usr/bin/env pythonw
"""
ViriaRevive Desktop App

Launch this file to start the GUI without a console window:
    double-click  app.pyw     (Windows hides the terminal automatically)
    pythonw app.pyw            (explicit)
"""

import sys
import webview
from pathlib import Path
from api_bridge import ApiBridge
from tray import TrayManager


def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


_force_closing = False


def main():
    global _force_closing
    start_minimized = "--minimized" in sys.argv or "--startup" in sys.argv

    api = ApiBridge()
    gui_dir = _get_base_dir() / "gui"

    window = webview.create_window(
        title="ViriaRevive",
        url=str(gui_dir / "index.html"),
        js_api=api,
        width=1100,
        height=750,
        min_size=(900, 600),
        resizable=True,
        background_color="#0a0a0f",
        minimized=start_minimized,
    )

    api._window = window

    tray = TrayManager(window, on_quit_callback=lambda: _force_quit(window, tray))

    def on_loaded():
        tray.start()
        # If launched with --minimized, hide to tray immediately
        if start_minimized:
            tray.on_minimize()

    def on_minimized():
        tray.on_minimize()

    def on_closing():
        if _force_closing:
            return True
        tray.on_minimize()
        return False

    window.events.loaded += on_loaded
    window.events.minimized += on_minimized
    window.events.closing += on_closing

    webview.start(debug=False)


def _force_quit(window, tray):
    global _force_closing
    _force_closing = True
    tray.stop()
    try:
        window.destroy()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
