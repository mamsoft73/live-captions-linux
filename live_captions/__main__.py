"""Entry point: python -m live_captions"""

import argparse
import os
import signal
import sys
import tempfile
from pathlib import Path

from .audio import MICROPHONE, SYSTEM
from .models import ALL_MODELS, install


def parse_args():
    parser = argparse.ArgumentParser(description="Live English captions for Linux")
    parser.add_argument("--source", choices=[SYSTEM, MICROPHONE], help="audio to caption")
    parser.add_argument(
        "--download-only", action="store_true", help="fetch the speech models and exit"
    )
    return parser.parse_args()


def show_download_progress(fraction):
    print(f"\r{fraction:.1%}", end="", flush=True)


def run_window(args):
    # Keep-above needs X11; see CaptionWindow. Must be set before QApplication.
    os.environ["QT_QPA_PLATFORM"] = "xcb"

    from PyQt5.QtCore import QLockFile
    from PyQt5.QtWidgets import QApplication

    from .engine import CaptionWorker
    from .overlay import CaptionWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Live Captions")

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    lock = QLockFile(str(Path(runtime_dir) / "live-captions.lock"))
    if not lock.tryLock(0):
        return 0

    window = CaptionWindow(args.source)
    worker = CaptionWorker(window.source, window.track_speakers)
    worker.caption.connect(window.set_caption)
    worker.status.connect(window.set_status)
    window.source_changed.connect(worker.set_source)
    window.paused_changed.connect(worker.set_paused)
    window.clear_requested.connect(worker.clear)
    window.track_speakers_changed.connect(worker.set_track_speakers)
    app.aboutToQuit.connect(worker.stop)

    window.show()
    worker.start()
    return app.exec_()


def main():
    args = parse_args()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if args.download_only:
        for model in ALL_MODELS:
            install(model, show_download_progress)
            print()
        return 0
    return run_window(args)


if __name__ == "__main__":
    sys.exit(main())
