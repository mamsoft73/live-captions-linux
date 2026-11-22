# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Always-on-top, on-device live English captions for Linux, similar to Windows Live Captions. There is no test suite or linter. The release archive's version comes from `pyproject.toml`; README's install example names it too.

## Commands

```bash
./install.sh                          # venv, models, desktop entry (no sudo)
.venv/bin/python -m live_captions     # run; add --source microphone for the mic
./make-release.sh                     # release/live-captions-<version>.tar.gz + .sha256
./uninstall.sh                        # remove venv, models, settings, menu entry
```

## Architecture

The pipeline is `audio.py` -> `engine.py` -> `overlay.py`:

- `audio.py` spawns `pw-record` (or `parec`) and reads raw 16 kHz s16le from its stdout.
- `engine.py` runs `CaptionWorker`, a `QThread` that feeds sherpa-onnx, punctuates, and emits the caption through Qt signals as a list of `(speaker, text)` turns. The worker owns all recognizer state; UI-thread calls only set flags or end the capture.
- `speakers.py` runs `SpeakerTracker`, a plain thread fed the same samples. It cuts them into phrases at pauses, embeds each one, and reports `(sample position, speaker)` about 2.3 s after a voice changes. The worker then splits text already on screen at that position, using token timestamps. Captions never wait for it, and a speaker change never resets the recognizer.
- `translation.py` translates the sentences on screen into Arabic on demand, with Argos Translate's CTranslate2 model. `overlay.py` runs it on a short-lived thread, shows the Arabic in place of the captions, and returns to English when the word count grows or the window is clicked.
- `overlay.py` draws only the last lines of the caption that fit, in `paintEvent`, with one `QTextLayout` format per turn and U+2028 between turns.
- `models.py` downloads resumably into `~/.local/share/live-captions/models/`. The Argos host rejects the default `Python-urllib` user agent with 403.

## Decisions backed by measurements

Each of these looks like something to "improve", but was tested on an i5-13420H. Measure before reversing one.

- **Fast-conformer, not Nemotron.** Nemotron (0.6b, 160 ms) was perfect on the test clips and punctuates, but ran at 0.65x real time and fell 11.5 s behind with all cores busy. Fast-conformer stayed under 100 ms behind.
- **`NUM_THREADS = 1`.** Two threads cost 218% of a core at idle against 31%, because ONNX Runtime's workers spin, and gained about 10 ms.
- **Endpoint rules in `_load_models`.** A reset discards undecoded audio and damages any word starting at that moment. Resetting after 0.6 s of silence made word errors 20 times more frequent than after 2 s. Resets during idle silence corrupted the first word in 4 of 18 cases, so rule 1 defers to rule 3. Memory stays flat without resets.
- **Trailing punctuation is stripped from live text.** Only `_commit` ends a sentence, because the last word can still change.
- **3D-Speaker "advanced" CAM++, not wespeaker CAM++.** On 1.5 s of real conversation, AUC was 0.91 against 0.52. Both cost about 22 ms per phrase.
- **Phrases cut at pauses, not sliding windows.** Windows blend two voices at each turn: 67% of words right against 63% for assuming one speaker.
- **Unsure phrases are held, never learned.** Teaching them to the current voice blended two people, after which it matched both and no second speaker was ever created.
- **`NEW_SPEAKER_SECONDS`.** Naming someone new from one short phrase caused up to 3.5 false breaks a minute over noise or music. With 1.5 s of speech there were none in 21.6 minutes, and none in 53.8 clean minutes from speakers unseen in tuning.
- **A thread, not a process.** `compute` releases the GIL. In the real app, word latency, backlog with all cores busy, and idle CPU were unchanged with tracking on. It adds about 1% of a core during speech and 46 MB.
- **Scoring by word, on real and built clips.** Built dialogues reach 90%, but their speakers were recorded in different rooms, which flatters the model. Real clips range from 97% to no gain at all, so judge changes on both.

- **Argos Translate, not NLLB-200 600M.** Argos is 88 MB, trained on film subtitles, and translates a screenful in 0.1 s. NLLB is 622 MB, about 45 minutes on this connection.
- **One sentence per translation input.** Given several sentences at once, the Argos model translates the first and drops the rest.
- **Translation stays loaded after first use.** Unloading freed little memory back to the system, about 120 of 160 MB stayed resident.
- **Translation hides on more words, not on any update.** While the film is paused the recognizer still ends the sentence and the tracker can still split turns, which change the text but not its word count.

## Display constraints

- GNOME on Wayland ignores keep-above from native clients, so `QT_QPA_PLATFORM=xcb` is forced and the window is override-redirect. That is why it has no keyboard focus and no taskbar entry. Input must stay mouse-only.
- PyQt5 is deliberate: Qt 6's xcb plugin needs `libxcb-cursor0`, which is not installed by default and would require sudo.

## Testing notes

- Do not capture through the real default sink. Create a private null sink with `pw-cli create-node adapter`, pass `--target` to the recorder, and destroy the sink afterwards.
- `QTest.mouseMove` warps the real pointer and does nothing under Wayland. Send `QMouseEvent`s directly.
- Plain `pw-record -` writes headerless raw PCM, so there is nothing to skip.
- `window.grab()` shows the rendered window without the desktop screenshot permission.
- Multi-speaker audio with reference turns: `tests/data` and `tutorials/assets` in pyannote-audio, and the `speaker-segmentation-models` release of sherpa-onnx. LibriSpeech `dev-clean-2` gives long single voices for counting false breaks, and can be downloaded partly because the archive is sorted by speaker.
