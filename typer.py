"""Type text as real hardware-style keystrokes (scancodes) via SendInput.

Citrix Workspace forwards physical-looking key events to the remote session but
ignores the clipboard (when locked down) and usually drops KEYEVENTF_UNICODE
"packet" keys. So every character is converted to the key + Shift/AltGr combo
that produces it on the current keyboard layout and sent as scancode
key-down / key-up events, exactly like a person typing.
"""
import ctypes
import time
import unicodedata
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
VK_CAPITAL = 0x14

# (virtual key, scancode)
K_SHIFT, K_CTRL, K_ALT, K_ENTER, K_TAB = (0x10, 0x2A), (0x11, 0x1D), (0x12, 0x38), (0x0D, 0x1C), (0x09, 0x0F)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
user32.VkKeyScanExW.restype = ctypes.c_short
user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HKL]
user32.MapVirtualKeyExW.restype = wintypes.UINT
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = wintypes.HKL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short

# Characters Whisper likes to emit that have no key on a normal layout.
_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


def _send(scan=0, flags=0, vk=0):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        # Happens when the focused window runs as administrator and we do not.
        raise OSError("Windows blocked the keystroke (is the target window running as administrator?)")


def _foreground_layout():
    hwnd = user32.GetForegroundWindow()
    tid = user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
    return user32.GetKeyboardLayout(tid)


class Typer:
    """Citrix mis-orders Shift against letters when events arrive too fast, but
    plain keys tolerate much shorter timing. So modifiers get their own, longer
    delay and stay held across runs of characters that need them."""

    def __init__(self, key_hold_ms=10, key_gap_ms=5, mode="scancode", modifier_delay_ms=40):
        self.hold = key_hold_ms / 1000.0
        self.gap = key_gap_ms / 1000.0
        self.mod_delay = modifier_delay_ms / 1000.0
        self.mode = mode
        self._held = []  # modifier keys currently down

    def _key(self, key, up=False):
        vk, scan = key
        if self.mode == "vk":      # virtual-key event that also carries its scancode
            _send(scan, KEYEVENTF_KEYUP if up else 0, vk)
        else:                      # pure scancode event
            _send(scan, KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0))

    def _set_mods(self, wanted):
        for k in [k for k in self._held if k not in wanted]:
            self._key(k, up=True)
            self._held.remove(k)
            time.sleep(self.mod_delay)
        for k in [k for k in wanted if k not in self._held]:
            self._key(k)
            self._held.append(k)
            time.sleep(self.mod_delay)

    def _tap(self, key, shift=False, ctrl=False, alt=False):
        self._set_mods([k for on, k in ((ctrl, K_CTRL), (alt, K_ALT), (shift, K_SHIFT)) if on])
        self._key(key)
        time.sleep(self.hold)
        self._key(key, up=True)
        time.sleep(self.gap)

    def _unicode(self, ch):
        self._set_mods([])
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            _send(unit, KEYEVENTF_UNICODE)
            _send(unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        time.sleep(self.gap)

    def _char(self, ch, hkl, caps):
        if ch == "\n":
            return self._tap(K_ENTER)
        if ch == "\t":
            return self._tap(K_TAB)
        if self.mode == "unicode":
            return self._unicode(ch)
        res = user32.VkKeyScanExW(ch, hkl)
        if res == -1:
            # No key for it on this layout: try an ASCII look-alike, else unicode packet.
            folded = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode()
            if folded and folded != ch:
                for c in folded:
                    self._char(c, hkl, caps)
            else:
                self._unicode(ch)
            return
        vk, state = res & 0xFF, (res >> 8) & 0xFF
        scan = user32.MapVirtualKeyExW(vk, 0, hkl)
        if not scan:
            return self._unicode(ch)
        shift = bool(state & 1)
        if caps and ch.isalpha():
            shift = not shift
        self._tap((vk, scan), shift=shift, ctrl=bool(state & 2), alt=bool(state & 4))

    def type(self, text):
        for old, new in _REPLACEMENTS.items():
            text = text.replace(old, new)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        hkl = _foreground_layout()
        caps = bool(user32.GetKeyState(VK_CAPITAL) & 1)
        try:
            for ch in text:
                self._char(ch, hkl, caps)
        finally:
            self._set_mods([])  # never leave Shift stuck down
