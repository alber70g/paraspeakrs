# Testing the 0.11.0 beta on Windows and Linux

`0.11.0b1` is the first build with the speakrs diarizer prebuilt for Windows x64 and
Linux x86_64, and with `paraspeakrs record` for meetings. It has passed CI on both, but
nobody has run it on a real Windows or Linux machine yet. That is what this is for.

## 1. Install

You need [uv](https://docs.astral.sh/uv/) and FFmpeg.

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install Gyan.FFmpeg
```

Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install ffmpeg          # or: sudo dnf install ffmpeg
```

Open a new terminal, then install the beta. Both version pins are needed: with only the
first, you get the beta without its diarizer.

```sh
uv tool install paraspeakrs==0.11.0b1 --with paraspeakrs-speakrs==0.11.0b1
paraspeakrs --version            # paraspeakrs 0.11.0b1
```

## 2. Transcribe a file

Any meeting recording works (`.m4a`, `.mp3`, `.wav`, ...). A stereo file with one
person per channel is the best case, but mono is fine.

```sh
paraspeakrs run --txt meeting.m4a
```

The first run asks which ASR model to download (FP32 2.4 GB is the best; INT8 490 MB is
the quickest to try) and fetches the diarization models (about 33 MB). Please time the
run, because diarization speed on a CPU is still unknown outside CI:

```powershell
Measure-Command { paraspeakrs run --txt meeting.m4a }      # Windows
```

```sh
time paraspeakrs run --txt meeting.m4a                     # Linux
```

Run it twice, so the second time shows the speed without downloads, and note the length
of the recording and your CPU.

## 3. Record a meeting

```sh
paraspeakrs record --list-devices
paraspeakrs record --seconds 60
```

While it records, talk into your microphone and play something with voices (a video, or
a real call). When it stops it saves `~/Documents/paraspeakrs-recordings/<date>_<time>.m4a`
and transcribes it into a `.txt` next to it. Check that:

- the transcript has an `L_` speaker for you and `R_` speakers for the other voices;
- in the `.m4a`, your voice is on the left channel and the other audio on the right;
- the file is as long as the recording (60 s here).

Pressing Enter or Ctrl+C instead of `--seconds` should also stop it and save.

On Windows, if the call plays through a headset that is not the default output, pass its
name with `--system` (see `--list-devices`).

## 4. Report back

Open an issue at <https://github.com/alber70g/paraspeakrs/issues> with:

- Windows version, or Linux distribution and desktop;
- on Linux, the sound server: `pactl info | grep "Server Name"`;
- the output of `paraspeakrs record --list-devices`;
- the timings from step 2;
- any error, with the whole message.

## Back to the stable release

```sh
uv tool install --force paraspeakrs
```
