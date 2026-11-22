"""English to Arabic on this computer, for the sentences on screen."""

import re
import threading

from .models import TRANSLATION, model_path

SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
ARABIC_MARKS = {".": ".", "?": "؟", "!": "!"}
BEAM_SIZE = 4
MAX_TOKENS = 256


class ArabicTranslator:
    """Loads its model on first use, so that captions start no slower."""

    def __init__(self):
        self._lock = threading.Lock()
        self._translator = None
        self._pieces = None

    def translate(self, texts):
        """Return the Arabic for each text. Safe to call from any thread."""
        # Given several sentences at once, the model translates the first and
        # drops the rest, so every sentence goes in separately.
        sentences = [[s for s in SENTENCE_END.split(text.strip()) if s] for text in texts]
        flat = [sentence for group in sentences for sentence in group]
        with self._lock:
            self._load()
            tokens = [self._pieces.encode(sentence, out_type=str) for sentence in flat]
            results = self._translator.translate_batch(
                tokens, beam_size=BEAM_SIZE, max_decoding_length=MAX_TOKENS
            )
        arabic = iter(
            _keep_end_mark(sentence, self._pieces.decode(result.hypotheses[0]))
            for sentence, result in zip(flat, results)
        )
        return [" ".join(next(arabic) for _ in group) for group in sentences]

    def _load(self):
        if self._translator is not None:
            return
        import ctranslate2
        import sentencepiece

        path = model_path(TRANSLATION)
        # One thread translates a screenful in about 0.1 s. A second saved 40 ms
        # but then kept using CPU while idle.
        self._translator = ctranslate2.Translator(
            str(path), device="cpu", inter_threads=1, intra_threads=1
        )
        self._pieces = sentencepiece.SentencePieceProcessor(
            model_file=str(path / "sentencepiece.model")
        )


def _keep_end_mark(english, arabic):
    """The model often drops the full stop, which runs sentences together."""
    mark = ARABIC_MARKS.get(english[-1:])
    if mark and not arabic.endswith((".", "؟", "!", "?")):
        arabic += mark
    return arabic
