"""Streaming speech recognition on a background thread."""

import numpy as np
import sherpa_onnx
from PyQt5.QtCore import QThread, pyqtSignal

from .audio import BYTES_PER_SAMPLE, SAMPLE_RATE, open_capture
from .models import ALL_MODELS, PUNCTUATION, RECOGNIZER, install, model_path
from .speakers import SpeakerTracker, load_extractor

FEATURE_DIM = 80
# One thread keeps up at 0.2x real time, even with every core busy. A second one
# barely helps latency, and ONNX Runtime's spinning workers cost two idle cores.
NUM_THREADS = 1
READ_BYTES = 4096
HISTORY_CHARS = 1500
RETRY_MS = 1000

STATUS_LOADING = "جارٍ تحميل نموذج التعرف على الكلام…"
STATUS_READY = "جاهز، بانتظار كلام باللغة الإنكليزية…"
STATUS_PAUSED = "متوقف مؤقتًا"
STATUS_NO_AUDIO = "تعذر التقاط الصوت، جارٍ إعادة المحاولة…"


class Stopped(Exception):
    pass


def _text_before(result, seconds):
    """Return how many characters of the recognized text come before a moment."""
    text = ""
    for token, stamp in zip(result.tokens, result.timestamps):
        if stamp >= seconds and token.startswith(" "):
            break
        text += token
    return len(text.lstrip())


class CaptionWorker(QThread):
    caption = pyqtSignal(object)
    status = pyqtSignal(str)

    def __init__(self, source, track_speakers=True):
        super().__init__()
        self._source = source
        self._track_speakers = track_speakers
        self._capture = None
        self._punctuation = None
        self._extractor = None
        self._tracker = None
        self._tracker_outdated = True
        self._restarting = False
        self._audio_lost = False
        self._running = True
        self._paused = False
        self._clear = False
        self._turns = [[0, ""]]
        self._spoken = ""
        self._turn_start = 0
        self._position = 0
        self._shown = None

    def set_source(self, source):
        self._source = source
        self._restarting = True
        self._end_capture()

    def set_paused(self, paused):
        self._paused = paused
        self.status.emit(STATUS_PAUSED if paused else STATUS_READY)

    def set_track_speakers(self, enabled):
        self._track_speakers = enabled
        self._tracker_outdated = True

    def clear(self):
        self._clear = True

    def stop(self):
        self._running = False
        self._end_capture()
        self.wait()
        if self._tracker:
            self._tracker.stop()

    def run(self):
        try:
            recognizer = self._load_models()
        except Stopped:
            return
        except Exception as error:
            self.status.emit(f"تعذر تحميل النموذج: {error}")
            return

        stream = recognizer.create_stream()
        self.status.emit(STATUS_READY)
        while self._running:
            try:
                self._capture = open_capture(self._source)
            except (OSError, RuntimeError) as error:
                self.status.emit(f"تعذر التقاط الصوت: {error}")
                return
            self._recognize(recognizer, stream)
            self._end_capture()
            if self._running and not self._restarting:
                self._audio_lost = True
                self.status.emit(STATUS_NO_AUDIO)
                self.msleep(RETRY_MS)
            self._restarting = False

    def _load_models(self):
        for model in ALL_MODELS:
            install(model, self._report_download)
        self.status.emit(STATUS_LOADING)

        punctuation = model_path(PUNCTUATION)
        self._punctuation = sherpa_onnx.OnlinePunctuation(
            sherpa_onnx.OnlinePunctuationConfig(
                model_config=sherpa_onnx.OnlinePunctuationModelConfig(
                    cnn_bilstm=str(punctuation / "model.int8.onnx"),
                    bpe_vocab=str(punctuation / "bpe.vocab"),
                    num_threads=1,
                )
            )
        )
        if self._track_speakers:
            self._extractor = load_extractor()

        recognizer = model_path(RECOGNIZER)
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(recognizer / "tokens.txt"),
            encoder=str(recognizer / "encoder.int8.onnx"),
            decoder=str(recognizer / "decoder.int8.onnx"),
            joiner=str(recognizer / "joiner.int8.onnx"),
            num_threads=NUM_THREADS,
            sample_rate=SAMPLE_RATE,
            feature_dim=FEATURE_DIM,
            decoding_method="greedy_search",
            # A reset drops audio the model has not decoded yet, damaging any word
            # that starts at that moment. So never reset during idle silence (rule 1
            # defers to rule 3), and only after long pauses in speech: resetting after
            # 0.6 s made word errors 20 times more frequent than after 2 s.
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=120.0,
            rule2_min_trailing_silence=2.0,
            rule3_min_utterance_length=120.0,
        )

    def _report_download(self, fraction):
        if not self._running:
            raise Stopped
        self.status.emit(f"جارٍ تنزيل نماذج التعرف على الكلام… {fraction:.0%}")

    def _recognize(self, recognizer, stream):
        """Feed captured audio to the recognizer until the capture ends."""
        pending = b""
        while self._running:
            data = self._capture.stdout.read(READ_BYTES)
            if not data:
                return
            if self._audio_lost:
                self._audio_lost = False
                self.status.emit(STATUS_PAUSED if self._paused else STATUS_READY)
            data = pending + data
            usable = len(data) - len(data) % BYTES_PER_SAMPLE
            pending = data[usable:]
            if self._clear:
                self._clear = False
                self._turns = [[0, ""]]
                self._spoken = ""
                self._turn_start = 0
                self._tracker_outdated = True
                recognizer.reset(stream)
                self._publish([])
            if self._tracker_outdated:
                self._tracker_outdated = False
                self._restart_tracker()
            if self._paused:
                continue

            samples = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32) / 32768
            stream.accept_waveform(SAMPLE_RATE, samples)
            if self._tracker:
                self._tracker.feed(self._position, samples)
            self._position += len(samples)
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)

            spoken = recognizer.get_result(stream).strip()
            speaker_changed = self._apply_speaker_changes(recognizer, stream, spoken)
            if recognizer.is_endpoint(stream):
                self._commit(spoken[self._turn_start:])
                recognizer.reset(stream)
                self._turn_start = 0
                spoken = ""
            if speaker_changed or spoken != self._spoken:
                self._spoken = spoken
                self._publish(self._visible_turns(spoken))

    def _restart_tracker(self):
        """Start listening afresh; a new tracker has forgotten every voice."""
        if self._tracker:
            self._tracker.stop()
            self._tracker = None
        if self._track_speakers:
            self._extractor = self._extractor or load_extractor()
            self._tracker = SpeakerTracker(self._extractor)
            self._tracker.start()

    def _apply_speaker_changes(self, recognizer, stream, spoken):
        """Break the text where the tracker heard a new voice, however long ago that was."""
        if not self._tracker or not self._tracker.changes:
            return False
        result = recognizer.get_result_all(stream)
        while self._tracker.changes:
            position, speaker = self._tracker.changes.popleft()
            boundary = _text_before(result, position / SAMPLE_RATE - result.start_time)
            self._start_turn(speaker, spoken, max(boundary, self._turn_start))
        return True

    def _start_turn(self, speaker, spoken, boundary):
        """End the open turn at a point in the text; the rest belongs to the new speaker."""
        self._commit(spoken[self._turn_start : boundary])
        self._turn_start = boundary
        if self._turns[-1][1]:
            self._turns.append([speaker, ""])
        else:
            self._turns[-1][0] = speaker

    def _visible_turns(self, spoken):
        """Return every turn as (speaker, text), with speech in progress on the last one."""
        speaker, finished = self._turns[-1]
        current = f"{finished} {self._open_sentence(spoken[self._turn_start :])}".strip()
        turns = [tuple(turn) for turn in self._turns[:-1]]
        return turns + [(speaker, current)] if current else turns

    def _open_sentence(self, spoken):
        """Punctuate speech still in progress, leaving its end open."""
        spoken = spoken.strip()
        if not spoken:
            return ""
        return self._punctuation.add_punctuation_with_case(spoken).rstrip(",.?!")

    def _commit(self, spoken):
        """Finish a sentence at a long pause or a change of speaker."""
        spoken = spoken.strip()
        if not spoken:
            return
        text = self._punctuation.add_punctuation_with_case(spoken).rstrip(",")
        if not text.endswith((".", "?", "!")):
            text += "."
        self._turns[-1][1] = f"{self._turns[-1][1]} {text}".strip()
        self._trim_history()

    def _trim_history(self):
        excess = sum(len(text) for _, text in self._turns) - HISTORY_CHARS
        while excess > 0 and len(self._turns) > 1:
            excess -= len(self._turns.pop(0)[1])
        if excess > 0:
            self._turns[0][1] = self._turns[0][1][excess:]

    def _publish(self, turns):
        if turns != self._shown:
            self._shown = turns
            self.caption.emit(turns)

    def _end_capture(self):
        capture = self._capture
        if capture is not None and capture.poll() is None:
            capture.terminate()
