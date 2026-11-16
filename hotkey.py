"""Global key listener built on Raw Input.

Raw Input is used instead of a keyboard hook because Citrix Workspace installs
its own low-level hook and swallows keys while its window is focused; hooks from
other apps (pynput, `keyboard`, AutoHotkey) often stop firing. Raw Input is
delivered to us regardless of which window has focus.
"""
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEKEYBOARD = 1
RI_KEY_BREAK, RI_KEY_E0 = 1, 2
PM_REMOVE = 1
HWND_MESSAGE = wintypes.HWND(-3)
LRESULT = ctypes.c_ssize_t

# name -> (scancode, is_extended)
KEYS = {
    "right_ctrl": (0x1D, True), "left_ctrl": (0x1D, False),
    "right_alt": (0x38, True), "left_alt": (0x38, False),
    "right_shift": (0x36, False), "right_win": (0x5C, True),
    "menu": (0x5D, True), "insert": (0x52, True),
    "caps_lock": (0x3A, False), "scroll_lock": (0x46, False),
    "f1": (0x3B, False), "f2": (0x3C, False), "f3": (0x3D, False), "f4": (0x3E, False),
    "f5": (0x3F, False), "f6": (0x40, False), "f7": (0x41, False), "f8": (0x42, False),
    "f9": (0x43, False), "f10": (0x44, False), "f11": (0x57, False), "f12": (0x58, False),
}

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
                ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def run(on_key):
    """Block forever, calling on_key(scancode, extended, is_down) for every
    physical key event (injected events, including our own typing, are skipped)."""

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                raw = RAWINPUT()
                size = wintypes.UINT(ctypes.sizeof(raw))
                got = user32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(raw), ctypes.byref(size),
                                             ctypes.sizeof(RAWINPUTHEADER))
                if (got != 0xFFFFFFFF and raw.header.dwType == RIM_TYPEKEYBOARD
                        and raw.header.hDevice):  # hDevice is NULL for injected input
                    kb = raw.keyboard
                    on_key(kb.MakeCode, bool(kb.Flags & RI_KEY_E0), not (kb.Flags & RI_KEY_BREAK))
            except Exception as exc:  # never let an exception escape into Win32
                print(f"[hotkey] {exc!r}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)  # keep a reference alive for the lifetime of the loop
    hinst = kernel32.GetModuleHandleW(None)
    cls = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName="VoiceKeysRawInput")
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(0, cls.lpszClassName, "VoiceKeys", 0, 0, 0, 0, 0,
                                  HWND_MESSAGE, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd)  # generic desktop / keyboard
    if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
        raise ctypes.WinError(ctypes.get_last_error())

    msg = wintypes.MSG()
    while True:  # Peek + sleep (instead of GetMessage) so Ctrl+C still works
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.005)
