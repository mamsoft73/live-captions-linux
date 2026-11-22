"""Telling voices apart, on a thread of its own so that captions never wait for it."""

import threading
import time
from collections import deque

import numpy as np
import sherpa_onnx

from .audio import SAMPLE_RATE
from .models import SPEAKER, model_path

FRAME_SAMPLES = SAMPLE_RATE // 50
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
POLL_SECONDS = 0.05
BACKLOG_CHUNKS = 2000

# A frame is voiced when louder than a share of the recent peak.
VOICED_SHARE = 0.1
VOICED_FLOOR = 0.003
PEAK_DECAY = 0.9995

# Phrases end at pauses, because voices change there and one voice per phrase is
# what makes an embedding reliable. Sliding windows blended two voices at each turn
# and barely beat assuming a single speaker.
PAUSE_FRAMES = 8
MIN_PHRASE_FRAMES = 30
MAX_PHRASE_FRAMES = 100

# Scores are the cosine between a phrase and the running mean of a voice.
MAX_SPEAKERS = 6
KNOWN_SCORE = 0.35
KNOWN_MARGIN = 0.1
SAME_SCORE = 0.45
# Naming someone new takes this much agreeing speech. Deciding from one short phrase
# broke a single voice up to 3.5 times a minute over music, against none at this length.
NEW_SPEAKER_SECONDS = 1.5
NEW_SPEAKER_SCORE = 0.45


def load_extractor():
    model = model_path(SPEAKER) / SPEAKER.files[0]
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1)
    )


class SpeakerBook:
    """The voices heard so far, and which of them is speaking."""

    def __init__(self):
        self._voices = []
        self._last_heard = []
        self._current = None
        self._unsure = []

    def observe(self, vector, seconds, position):
        """Take one phrase; return (position, speaker) if the voice changed."""
        phrase = (vector, seconds, position)
        if self._current is None:
            self._change_to(self._add_voice(vector), [phrase])
            return None

        known = self._switch_target(vector)
        if known is not None:
            return self._change_to(known, [phrase])
        if self._score(self._current, vector) >= SAME_SCORE:
            self._learn(self._current, [phrase])
            self._unsure = []
            return None

        # Unsure. Teaching the current voice now could blend two people, after which
        # it would match them both, so hold the phrase until there is enough to judge.
        self._unsure.append(phrase)
        while sum(held[1] for held in self._unsure[1:]) >= NEW_SPEAKER_SECONDS:
            del self._unsure[0]
        if sum(held[1] for held in self._unsure) < NEW_SPEAKER_SECONDS:
            return None

        mean = sum(held[0] * held[1] for held in self._unsure)
        mean = mean / (np.linalg.norm(mean) + 1e-9)
        known = self._switch_target(mean)
        if known is not None:
            return self._change_to(known, self._unsure)
        if self._score(self._current, mean) < NEW_SPEAKER_SCORE:
            return self._change_to(self._add_voice(vector), self._unsure)
        self._learn(self._current, self._unsure)
        self._unsure = []
        return None

    def _switch_target(self, vector):
        """Return another known voice that this clearly belongs to, if any."""
        scores = [self._score(voice, vector) for voice in range(len(self._voices))]
        best = int(np.argmax(scores))
        clearly_better = scores[best] - scores[self._current] >= KNOWN_MARGIN
        if best != self._current and scores[best] >= KNOWN_SCORE and clearly_better:
            return best
        return None

    def _change_to(self, voice, phrases):
        self._learn(voice, phrases)
        self._current = voice
        self._unsure = []
        return phrases[0][2], voice

    def _add_voice(self, vector):
        """Make room for a voice; after a long session, the oldest one gives way."""
        if len(self._voices) < MAX_SPEAKERS:
            self._voices.append(np.zeros_like(vector))
            self._last_heard.append(0)
            return len(self._voices) - 1
        others = [voice for voice in range(MAX_SPEAKERS) if voice != self._current]
        voice = min(others, key=self._last_heard.__getitem__)
        self._voices[voice] = np.zeros_like(vector)
        return voice

    def _learn(self, voice, phrases):
        for vector, seconds, position in phrases:
            self._voices[voice] += vector * seconds
            self._last_heard[voice] = position

    def _score(self, voice, vector):
        total = self._voices[voice]
        return float(total @ vector) / (float(np.linalg.norm(total)) + 1e-9)


class SpeakerTracker(threading.Thread):
    """Listen to the same audio as the recognizer and report changes of voice.

    Each report arrives about two seconds after the change, and names the sample
    position where it happened. The caption thread only appends to one deque and
    reads another, and embedding releases the GIL.
    """

    def __init__(self, extractor):
        super().__init__(daemon=True)
        self.changes = deque()
        self._extractor = extractor
        self._audio = deque(maxlen=BACKLOG_CHUNKS)
        self._running = True
        self._book = SpeakerBook()
        self._leftover = np.zeros(0, dtype=np.float32)
        self._leftover_position = 0
        self._peak = 0.0
        self._phrase = []
        self._phrase_position = 0
        self._quiet_frames = 0

    def feed(self, position, samples):
        self._audio.append((position, samples))

    def stop(self):
        self._running = False
        self.join()

    def run(self):
        while self._running:
            if self._audio:
                self._take(*self._audio.popleft())
            else:
                time.sleep(POLL_SECONDS)

    def _take(self, position, samples):
        if len(self._leftover) and self._leftover_position + len(self._leftover) == position:
            samples = np.concatenate((self._leftover, samples))
            position = self._leftover_position
        usable = len(samples) - len(samples) % FRAME_SAMPLES
        for offset in range(0, usable, FRAME_SAMPLES):
            self._take_frame(position + offset, samples[offset : offset + FRAME_SAMPLES])
        self._leftover = samples[usable:]
        self._leftover_position = position + usable

    def _take_frame(self, position, frame):
        level = float(np.sqrt(np.mean(frame * frame)))
        self._peak = max(level, self._peak * PEAK_DECAY)
        if level <= max(VOICED_FLOOR, VOICED_SHARE * self._peak):
            self._quiet_frames += 1
            if self._quiet_frames >= PAUSE_FRAMES and len(self._phrase) >= MIN_PHRASE_FRAMES:
                self._end_phrase()
            return

        if not self._phrase:
            self._phrase_position = position
        self._phrase.append(frame)
        self._quiet_frames = 0
        if len(self._phrase) >= MAX_PHRASE_FRAMES:
            self._end_phrase()

    def _end_phrase(self):
        samples = np.concatenate(self._phrase)
        seconds = len(self._phrase) * FRAME_SECONDS
        self._phrase = []
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if self._extractor.is_ready(stream):
            vector = np.array(self._extractor.compute(stream), dtype=np.float32)
            vector /= np.linalg.norm(vector) + 1e-9
            change = self._book.observe(vector, seconds, self._phrase_position)
            if change:
                self.changes.append(change)
