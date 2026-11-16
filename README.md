# winvoice

Voice typing for Windows that works inside Citrix / VDI sessions.

Hold a key, speak, release — the text is typed at the cursor as real keystrokes.
Speech is transcribed locally with Whisper, so no audio leaves your PC, and nothing
goes through the clipboard, so it works where copy-paste into the remote desktop is blocked.

It runs on your **local PC**, not inside the remote session.

## Install

You need Windows 10 or 11 and [Python 3.11 or newer](https://www.python.org/downloads/)
(tick "Add python.exe to PATH" in the installer). An NVIDIA GPU makes it faster but is optional.

1. Download the code: **Code → Download ZIP** on GitHub and unzip it, or

   ```
   git clone https://github.com/spbreed/winvoice.git
   ```

2. Double-click **`run.bat`** in the folder.

The first start takes a few minutes: it installs the Python packages into a local
`.venv` folder and downloads the speech model. Later starts take a few seconds.

## Run

1. Double-click `run.bat` and wait for the line `Ready.`
2. Click into any text field — for example a document inside Citrix.
3. Hold **Right Ctrl**, speak, then release. A beep marks start and stop, and the text types itself.

Say "new line" or "new paragraph" to press Enter. Close the window (or press Ctrl+C) to quit.

## Settings

Edit `config.toml` and restart. The most useful ones:

| Setting | What it does |
| --- | --- |
| `[hotkey] key` | Which key to hold (`right_ctrl`, `right_alt`, `f9`, ...) |
| `[hotkey] mode` | `hold` to talk while held, or `toggle` to tap on / tap off |
| `[audio] device` | Microphone to use; list them with `.venv\Scripts\python.exe dictate.py --list-devices` |
| `[whisper] model` | Speech model; `auto` picks a large one on GPU and a small one on CPU |
| `[whisper] prompt` | Names and jargon you say often, to improve spelling |
| `[typing]` timings | How fast keys are sent (see below) |

## If the text arrives wrong in Citrix

Citrix scrambles capitals and drops letters when keys arrive too fast. Find the fastest
speed your session accepts:

```
.venv\Scripts\python.exe dictate.py --calibrate
```

Click into Notepad inside Citrix during the countdown. The same sentence is typed five
times, fastest first. Copy the values printed for the lowest-numbered correct line into
the `[typing]` section of `config.toml`.

| Symptom | Fix |
| --- | --- |
| Wrong capitals, missing letters | Run `--calibrate` as above, or raise `modifier_delay_ms` |
| Wrong symbols (e.g. `"` becomes `@`) | Use the same keyboard layout on your PC and inside Citrix |
| Nothing arrives at all | Run `dictate.py --diagnose` to try other typing methods; set the one that works as `[typing] mode` |
| Hotkey does nothing | Choose another key under `[hotkey]` |

## How it works

- `dictate.py` — records the microphone, transcribes with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), runs the main loop
- `typer.py` — types each character as scancode key presses with `SendInput`, which Citrix forwards like a physical keyboard
- `hotkey.py` — listens for the hotkey through Raw Input, which keeps working while the Citrix window has focus
