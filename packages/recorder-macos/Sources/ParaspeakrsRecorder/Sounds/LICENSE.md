# Dictation sounds

The three tones were made by the project's author with the Musicca online synth,
recorded into one file, `musicca-synth-6s.mp3`, and cut into separate WAVs. They are
under this project's MIT license.

| File | Cut from `musicca-synth-6s.mp3` |
|---|---|
| `dictation-start.wav` | 0.07–0.87 s, the first tone (about 400 Hz) |
| `dictation-cancel.wav` | 2.02–2.82 s, the second tone (about 330 Hz) |
| `dictation-stop.wav` | 3.94–4.65 s, the third tone (about 265 Hz) |

All three: played 3× faster with the pitch kept (ffmpeg `atempo`), so 0.24–0.28 s
long; -3.9 dB, the same gain for each (peaks at -18 to -20 dBFS); a 5 ms fade-in
and a 20 ms fade-out; 48 kHz 16-bit stereo. The fourth tone in the
recording only kept it from stopping early and is not used.
