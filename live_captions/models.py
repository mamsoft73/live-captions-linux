"""Model registry and resumable download."""

import http.client
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
DOWNLOAD_BLOCK = 256 * 1024
DOWNLOAD_TIMEOUT = 30
DOWNLOAD_RETRIES = 10
RETRY_SECONDS = 3


@dataclass(frozen=True)
class Model:
    url: str
    name: str
    files: tuple


RECOGNIZER = Model(
    f"{RELEASES}/asr-models/sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8.tar.bz2",
    "sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8",
    ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"),
)
PUNCTUATION = Model(
    f"{RELEASES}/punctuation-models/sherpa-onnx-online-punct-en-2024-08-06.tar.bz2",
    "sherpa-onnx-online-punct-en-2024-08-06",
    ("model.int8.onnx", "bpe.vocab"),
)
SPEAKER = Model(
    # The release really is spelled this way.
    f"{RELEASES}/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
    ("3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",),
)
TRANSLATION = Model(
    # Argos Translate's English to Arabic, trained mostly on film subtitles.
    "https://argos-net.com/v1/translate-en_ar-1_0.argosmodel",
    "argos-translate-en-ar-1.0",
    ("model.bin", "shared_vocabulary.txt", "sentencepiece.model"),
)
ALL_MODELS = (RECOGNIZER, PUNCTUATION, SPEAKER, TRANSLATION)


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "live-captions"


def model_path(model):
    return data_dir() / "models" / model.name


def is_installed(model):
    return all((model_path(model) / name).is_file() for name in model.files)


def install(model, on_progress=lambda fraction: None):
    """Fetch and unpack a model, continuing any partial download."""
    if is_installed(model):
        return
    target = model_path(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    download = target.parent / f"{model.name}.part"
    _download(model.url, download, on_progress)

    unpacked = target.parent / f"{model.name}.unpacking"
    shutil.rmtree(unpacked, ignore_errors=True)
    unpacked.mkdir()
    if model.url.endswith(".tar.bz2"):
        with tarfile.open(download, "r|bz2") as archive:
            for member in archive:
                if member.isfile() and Path(member.name).name in model.files:
                    _unpack(archive.extractfile(member), unpacked / Path(member.name).name)
        download.unlink()
    elif model.url.endswith(".argosmodel"):  # A zip archive.
        with zipfile.ZipFile(download) as archive:
            for member in archive.infolist():
                if not member.is_dir() and Path(member.filename).name in model.files:
                    _unpack(archive.open(member), unpacked / Path(member.filename).name)
        download.unlink()
    else:
        download.rename(unpacked / model.files[0])
    shutil.rmtree(target, ignore_errors=True)
    unpacked.rename(target)


def _unpack(source, path):
    with source, open(path, "wb") as output:
        shutil.copyfileobj(source, output)


def _download(url, path, on_progress):
    """Download with retries; a slow or unstable connection must not lose progress."""
    failures = 0
    while True:
        size_before = _size(path)
        try:
            if _fetch_remaining(url, path, on_progress):
                return
        except (OSError, http.client.HTTPException) as error:
            last_error = error
        else:
            last_error = ConnectionError("connection closed early")
        failures = 0 if _size(path) > size_before else failures + 1
        if failures >= DOWNLOAD_RETRIES:
            raise last_error
        time.sleep(RETRY_SECONDS)


def _size(path):
    return path.stat().st_size if path.exists() else 0


def _fetch_remaining(url, path, on_progress):
    """Append the rest of the file; return whether it is now complete."""
    done = _size(path)
    # Some hosts refuse the default Python-urllib agent.
    headers = {"Range": f"bytes={done}-", "User-Agent": "live-captions"}
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT)
    except urllib.error.HTTPError as error:
        if error.code == 416:  # The partial file already holds every byte.
            return True
        raise
    with response, open(path, "ab" if response.status == 206 else "wb") as output:
        if response.status != 206:
            done = 0
        total = done + int(response.headers["Content-Length"])
        while block := response.read(DOWNLOAD_BLOCK):
            output.write(block)
            done += len(block)
            on_progress(done / total)
    return done >= total
