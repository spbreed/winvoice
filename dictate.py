"""VoiceKeys - hold a key, speak, and the text is typed as real keystrokes.

Runs on the local Windows PC (not inside Citrix). Speech is transcribed locally
with Whisper and typed with scancode key events, so it lands wherever the cursor
is - including a Citrix / VDI session with the clipboard locked down.
"""
import argparse
import collections
import os
import queue
import re
import site
import sys
import threading
import time
import tomllib
import winsound
from pathlib import Path

import numpy as np

import hotkey
from typer import Typer

HERE = Path(__file__).resolve().parent
RATE = 16000
BLOCK = 1600            # 100 ms audio blocks
PREROLL_BLOCKS = 3      # keep 300 ms before the key press so the first word isn't clipped
MIN_SECONDS = 0.4
MAX_SECONDS = 180


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def add_cuda_dll_dirs():
    """Make the pip-installed NVIDIA cuBLAS/cuDNN DLLs findable by ctranslate2."""
    for root in site.getsitepackages() + [site.getusersitepackages()]:
        nvidia = Path(root) / "nvidia"
        if nvidia.is_dir():
            for bin_dir in nvidia.glob("*/bin"):
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


class Transcriber:
    def __init__(self, cfg):
        add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        self.language = cfg.get("language") or None
        self.prompt = cfg.get("prompt") or None
        want_device = cfg.get("device", "auto")
        want_model = cfg.get("model", "auto")
        attempts = []
        if want_device in ("auto", "cuda"):
            attempts.append(("cuda", "float16", "large-v3-turbo"))
        if want_device in ("auto", "cpu"):
            attempts.append(("cpu", "int8", "small.en" if self.language == "en" else "small"))
        last_error = None
        for device, compute, default_model in attempts:
            name = default_model if want_model == "auto" else want_model
            try:
                log(f"Loading Whisper model '{name}' on {device} (first run downloads it)...")
                self.model = WhisperModel(name, device=device, compute_type=compute)
                self.transcribe(np.zeros(RATE, dtype=np.float32))  # warm up; surfaces missing CUDA DLLs
                log(f"Model ready on {device}.")
                return
            except Exception as exc:
                last_error = exc
                log(f"Could not use {device}: {exc}")
        raise SystemExit(f"No usable Whisper backend: {last_error}")

    def transcribe(self, audio):
        segments, _ = self.model.transcribe(
            audio, language=self.language, initial_prompt=self.prompt,
            vad_filter=True, beam_size=5, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()


def apply_commands(text, commands):
    for phrase, replacement in commands.items():
        pattern = r"[ \t]*\b" + re.escape(phrase) + r"\b[.,!?]?[ \t]*"
        text = re.sub(pattern, lambda _m, r=replacement: r, text, flags=re.IGNORECASE)
    return text


def make_typer(tp):
    return Typer(tp.get("key_hold_ms", 10), tp.get("key_gap_ms", 5), tp.get("mode", "scancode"),
                 tp.get("modifier_delay_ms", 40))


class App:
    def __init__(self, cfg):
        import sounddevice as sd

        self.cfg = cfg
        hk = cfg.get("hotkey", {})
        key = hk.get("key", "right_ctrl")
        if key not in hotkey.KEYS:
            raise SystemExit(f"Unknown hotkey '{key}'. Choose from: {', '.join(hotkey.KEYS)}")
        self.key_name = key
        self.key = hotkey.KEYS[key]
        self.toggle = hk.get("mode", "hold") == "toggle"
        tp = cfg.get("typing", {})
        self.typer = make_typer(tp)
        self.trailing_space = tp.get("trailing_space", True)
        self.commands = cfg.get("commands", {})
        self.beep = cfg.get("audio", {}).get("beep", True)

        self.transcriber = Transcriber(cfg.get("whisper", {}))

        self.lock = threading.Lock()
        self.recording = False
        self.key_down = False
        self.blocks = []
        self.preroll = collections.deque(maxlen=PREROLL_BLOCKS)
        self.jobs = queue.Queue()

        device = cfg.get("audio", {}).get("device", "") or None
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="float32",
                                     blocksize=BLOCK, device=device, callback=self._on_audio)
        self.stream.start()
        threading.Thread(target=self._worker, daemon=True).start()

    # --- audio -----------------------------------------------------------
    def _on_audio(self, indata, frames, time_info, status):
        block = indata[:, 0].copy()
        with self.lock:
            if self.recording:
                self.blocks.append(block)
                if len(self.blocks) * BLOCK > MAX_SECONDS * RATE:
                    self.recording = False
                    self.jobs.put(np.concatenate(self.blocks))
                    self.blocks = []
            else:
                self.preroll.append(block)

    def _sound(self, freq):
        if self.beep:
            threading.Thread(target=winsound.Beep, args=(freq, 70), daemon=True).start()

    def _start(self):
        with self.lock:
            if self.recording:
                return
            self.blocks = list(self.preroll)
            self.recording = True
        self._sound(880)
        log("Listening...")

    def _stop(self, cancel=False):
        with self.lock:
            if not self.recording:
                return
            self.recording = False
            blocks, self.blocks = self.blocks, []
        if cancel:
            log("Cancelled.")
            return
        self._sound(520)
        audio = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        if len(audio) < (MIN_SECONDS + PREROLL_BLOCKS * BLOCK / RATE) * RATE:
            log("Too short, ignored.")
            return
        self.jobs.put(audio)

    # --- hotkey ----------------------------------------------------------
    def on_key(self, scan, extended, down):
        if (scan, extended) != self.key:
            # In hold mode, another key while holding means the user is doing a
            # normal shortcut (e.g. Ctrl+C), not dictating.
            if down and not self.toggle and self.recording:
                self._stop(cancel=True)
            return
        if down == self.key_down:
            return  # auto-repeat
        self.key_down = down
        if self.toggle:
            if down:
                self._stop() if self.recording else self._start()
        elif down:
            self._start()
        else:
            self._stop()

    # --- transcribe + type -----------------------------------------------
    def _worker(self):
        while True:
            audio = self.jobs.get()
            try:
                started = time.time()
                text = self.transcriber.transcribe(audio)
                if not text:
                    log("(nothing heard)")
                    continue
                text = apply_commands(text, self.commands)
                if self.trailing_space and not text.endswith("\n"):
                    text += " "
                log(f"({time.time() - started:.1f}s) {text!r}")
                while self.key_down:  # never type while the hotkey modifier is still down
                    time.sleep(0.02)
                time.sleep(0.05)
                self.typer.type(text)
            except Exception as exc:
                log(f"Error: {exc!r}")

    def run(self):
        how = "tap to start/stop" if self.toggle else "hold to talk, release to type"
        log(f"Ready. Hotkey: {self.key_name} ({how}). Ctrl+C in this window to quit.")
        try:
            hotkey.run(self.on_key)
        except KeyboardInterrupt:
            log("Bye.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    ap.add_argument("--test-type", metavar="TEXT",
                    help="wait 5 s, then type TEXT at the cursor (tests Citrix typing without the mic)")
    ap.add_argument("--calibrate", action="store_true",
                    help="type the same line at 5 speeds, to find the fastest one Citrix accepts")
    ap.add_argument("--diagnose", action="store_true",
                    help="type one short line per typing method, to find which one reaches Citrix")
    ap.add_argument("--transcribe-file", metavar="WAV", help="transcribe a 16-bit WAV file, print the text, exit")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)

    if args.calibrate:
        # (key_hold_ms, key_gap_ms, modifier_delay_ms), fastest first
        speeds = [(3, 2, 15), (5, 5, 25), (10, 5, 40), (15, 10, 40), (25, 15, 50)]
        sample = "The Quick Brown Fox? Yes, it's 100% OK! (a_b@c.com)"
        for n in range(8, 0, -1):
            print(f"Click into Notepad inside Citrix and keep it focused... starting in {n}", flush=True)
            time.sleep(1)
        for i, (hold, gap, mod) in enumerate(speeds, 1):
            started = time.time()
            Typer(hold, gap, "scancode", mod).type(f"{i}: {sample}" + chr(10))
            print(f"line {i}: key_hold_ms={hold} key_gap_ms={gap} modifier_delay_ms={mod} "
                  f"-> {len(sample) / (time.time() - started):.0f} chars/s", flush=True)
            time.sleep(1.5)
        print("Done. Every line should read:  " + sample)
        print("Put the values of the lowest-numbered correct line into config.toml [typing].")
        return
    if args.diagnose:
        tests = [("scancode", 8, "one"), ("scancode", 40, "two"), ("scancode", 120, "three"),
                 ("vk", 40, "four"), ("unicode", 40, "five")]
        for n in range(8, 0, -1):
            print(f"Click into Notepad inside Citrix and keep it focused... starting in {n}", flush=True)
            time.sleep(1)
        for mode, ms, word in tests:
            print(f"typing '{word}' with mode={mode}, {ms} ms per key", flush=True)
            Typer(ms, ms, mode, ms).type(word + chr(10))
            time.sleep(1)
        print("Done. Which of the words one / two / three / four / five showed up in Notepad?")
        return
    if args.test_type is not None:
        tp = cfg.get("typing", {})
        typer = make_typer(tp)
        for n in range(5, 0, -1):
            print(f"Click into the target window... typing in {n}", flush=True)
            time.sleep(1)
        typer.type(args.test_type)
        return
    if args.transcribe_file:
        import wave
        with wave.open(args.transcribe_file, "rb") as wf:
            assert wf.getsampwidth() == 2, "need 16-bit PCM"
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            pcm = pcm.reshape(-1, wf.getnchannels()).mean(axis=1) / 32768.0
            src_rate = wf.getframerate()
        if src_rate != RATE:
            idx = np.arange(0, len(pcm), src_rate / RATE)
            pcm = np.interp(idx, np.arange(len(pcm)), pcm)
        text = Transcriber(cfg.get("whisper", {})).transcribe(pcm.astype(np.float32))
        print(apply_commands(text, cfg.get("commands", {})))
        return

    App(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
