"""Low-latency audio capture through the desktop's sound server."""

import shutil
import subprocess

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

SYSTEM = "system"
MICROPHONE = "microphone"


def capture_command(source):
    """Build the recorder command that writes raw s16le mono audio to stdout."""
    if shutil.which("pw-record"):
        command = [
            "pw-record",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            "--format=s16",
            "--latency=20ms",
            "--media-category=Capture",
            "--media-role=Accessibility",
        ]
        if source == SYSTEM:
            command += ["--properties", "{ stream.capture.sink = true }"]
        return command + ["-"]

    if shutil.which("parec"):
        device = "@DEFAULT_MONITOR@" if source == SYSTEM else "@DEFAULT_SOURCE@"
        return [
            "parec",
            f"--device={device}",
            "--format=s16le",
            f"--rate={SAMPLE_RATE}",
            "--channels=1",
            "--latency-msec=20",
        ]

    raise RuntimeError("Neither pw-record nor parec was found")


def open_capture(source):
    return subprocess.Popen(
        capture_command(source),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
