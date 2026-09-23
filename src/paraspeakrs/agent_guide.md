# paraspeakrs agent guide

Transcribe a recording and get the speakers named by the user, using only the
command line and files. No MCP server needed. Every command prints JSON on
stdout. Progress goes to stderr. An expected failure exits with code 2 and an
`error: …` line on stderr.

## The flow

### 1. Transcribe into a new directory

    paraspeakrs agent transcribe <audio-file> -o <out-dir>

`<out-dir>` must be new or empty. Transcribing takes minutes for a long
meeting, and identical audio reuses the earlier job instantly. The directory
gets:

| File | What it is |
|---|---|
| `transcript.txt` | `[HH:MM:SS] SPEAKER_00: text` lines, with a YAML header |
| `SPEAKER_00__NAME-ME.wav` | one sample per speaker: several takes (at least 8 s if they spoke that long), separated by a beep |
| `SPEAKER_01__Alice_0.82.wav` | the voice library recognized this speaker (score 0.82) |
| `NEXT_STEPS.md` | the renaming instructions, written for the user |
| `manifest.json` | bookkeeping for `apply`. Do not edit it |

The JSON output lists each speaker with `file`, `suggested_label`, `score`,
`talk_seconds` and `sample_lines` (their first few lines of speech).

### 2. Ask the user to name the speakers

You cannot hear the samples, but the user can. Tell them the directory and
point them at `NEXT_STEPS.md`. You can offer guesses from `sample_lines`
(people introduce themselves, or address each other by name). You may do the
renames yourself when the user tells you the names.

The part after `__` in each filename is the whole interface:

| Do this | Result |
|---|---|
| `SPEAKER_00__NAME-ME.wav` → `SPEAKER_00__Bob.wav` | named Bob; the voice library learns Bob's voice |
| leave `SPEAKER_01__Alice_0.82.wav` as is | the suggestion is accepted, and the library is not taught |
| `SPEAKER_01__Alice_0.82.wav` → `SPEAKER_01__Alice.wav` | confirmed; the library is taught |
| give two files the same name | merged into one person |
| delete the file | that speaker's lines are dropped from the transcript |
| leave `NAME-ME` | the transcript keeps the raw `SPEAKER_00` label |

Never change the part before `__`. A file without a known speaker ID, an empty
name, or two files for one speaker makes `apply` fail and change nothing.

### 3. Apply the names

    paraspeakrs agent apply <out-dir> --dry-run   # show what would happen
    paraspeakrs agent apply <out-dir>             # do it

This rewrites `transcript.txt` with the names. The JSON output gives each
speaker's `action` (`named`, `suggestion`, `unnamed` or `dropped`), plus any
`merged` names. It is safe to re-run: rename again, or restore a deleted file,
then apply again. The last state of the directory wins.

## Notes

- Read `transcript.txt` from disk, not from the command output. It can be long.
- The names are stored with the job in the shared paraspeakrs workspace, so the
  TUI and the MCP server see them too.
- The same pipeline flags as `paraspeakrs run` apply, e.g. `--workspace-dir`
  and `--sherpa-precision`. The first run downloads the ASR model. Pass
  `--sherpa-precision fp32` to avoid the interactive precision question.
