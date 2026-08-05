"""
Jarvis OS — Desktop control (Feature 2)

The OS abstraction behind `app/tools/desktop_tools.py`. Phase 8 taught Jarvis to
SENSE the desktop (active app, window title, idle time, screen OCR) and gave it
no way to ACT on any of it; this closes that asymmetry.

⚠️ WHY ctypes AND NOT A POWERSHELL HELPER (a deliberate departure from the spec)
--------------------------------------------------------------------------------
FEATURES.md specified a long-lived PowerShell process using `Add-Type` P/Invoke,
reasoning from `electron/sensing.ts` — which is right THERE, because its host is
Node and Node has no FFI without a native module. Its host here is Python, and
`ctypes` calls the identical Win32 functions with no subprocess at all. That is
strictly better on every axis the spec cared about:

  - No process to spawn, supervise, restart or orphan. The spec's own risk note
    ("must be a single long-lived process, not a spawn per call — otherwise every
    volume change is a ~200ms process launch") simply does not arise.
  - **No shell, therefore no injection surface.** A PowerShell helper reading
    commands on stdin is an arbitrary-code channel the moment any field of that
    command is model-derived; keeping it safe would mean a fixed verb table and a
    typed parser — i.e. re-deriving what a direct function call already is.
  - Still zero new dependency, which was the spec's stated reason for PowerShell.

`Pillow` (screenshots) is imported LAZILY: it arrives transitively with
`fastembed`, so it is present in any working install, but a missing copy must
cost the screenshot tool and nothing else (`browser/observe.py`'s convention).

Design
------
- `DesktopController` is the seam. `WindowsDesktopController` implements it with
  ctypes; `UnsupportedDesktopController` answers every call with a clean
  "not supported on this platform" rather than an ImportError at module load, so
  a macOS/Linux dev box imports and tests this file normally.
- `DESKTOP_CONTROLLER_FACTORY` is the injectable seam (the `STT_MODEL_FACTORY` /
  `HOME_SERVICE_FACTORY` pattern), resolved at CALL time. The conftest autouse
  fixture installs a refusing fake suite-wide — non-negotiable, or a test run
  starts closing the developer's windows.
- `DesktopError` is the one failure type tools catch. Every method raises it
  rather than leaking a ctypes/OSError.

The app registry is what makes `launch_app` safe
------------------------------------------------
`launch_app` must NOT be a thin ShellExecute over a model-supplied string — that
is `run_command` with the approval gate weakened, which is strictly worse than
`run_command`. Instead `discover_apps()` enumerates the Start Menu ONCE into a
registry of (display name → .lnk path), and a launch resolves a NAME against
that registry. The reachable surface is therefore exactly "applications this
user has installed", a bounded and inspectable set — the property `run_command`
cannot have. No path, no command line, no arguments, ever.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401 — needed for the Win32 prototypes below
import os
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

# ------------------------------------------------------------------ constants

#: Windows are enumerated once per call; a machine with hundreds of top-level
#: windows would otherwise put an unreadable wall into a planner prompt.
MAX_WINDOWS = 60

#: Discovered applications. Large Start Menus exist (this dev box has ~200).
MAX_APPS = 400

#: The registry is rebuilt at most this often — installing an app mid-session is
#: rare, and a Start Menu walk is ~150ms of disk.
APP_CACHE_SECONDS = 300.0

#: Fuzzy app-name resolution, borrowed verbatim from the memory engine's contact
#: identity resolution so the two behave the same way. A match under MIN_SCORE
#: is no match; two matches within MIN_GAP of each other are AMBIGUOUS and the
#: plan must ask (rule 11) rather than pick between "Code" and "Code - Insiders".
APP_MIN_SCORE = 81.0
APP_MIN_GAP = 8.0

#: Screenshots live here, and only a PATH ever leaves this module.
SCREENSHOT_DIR_NAME = "screenshots"

MEDIA_ACTIONS = ("play_pause", "next", "previous", "stop")

VOLUME_MIN = 0
VOLUME_MAX = 100


class DesktopError(Exception):
    """A desktop operation failed. Carries a user-facing message: it is rendered
    straight into a failed ToolResult, so it must read as an explanation rather
    than a stack trace."""


@dataclass(frozen=True)
class Window:
    """One visible top-level window.

    `handle` is the OS window handle — an opaque integer, and the identifier the
    planner's window-handle lock grounds against. `process` is carried because
    it is STABLE for a window's lifetime and a title is not.
    """

    handle: int
    title: str
    process: str


@dataclass(frozen=True)
class AppEntry:
    """One launchable application: the display name a user would say, and the
    Start Menu shortcut that launches it. `path` is discovered from the
    filesystem and is never model-derived."""

    name: str
    path: str


@dataclass(frozen=True)
class VolumeState:
    level: int  # 0-100
    muted: bool


# ============================================================ the seam

class DesktopController:
    """What a desktop must be able to do. Every method raises `DesktopError` on
    failure — never a ctypes error, never an OSError."""

    platform_name: str = "unknown"

    def list_windows(self) -> list[Window]:
        raise NotImplementedError

    def window(self, handle: int) -> Optional[Window]:
        """The live window for a handle, or None if it no longer exists."""
        raise NotImplementedError

    def focus_window(self, handle: int) -> None:
        raise NotImplementedError

    def close_window(self, handle: int) -> None:
        raise NotImplementedError

    def launch(self, path: str) -> None:
        raise NotImplementedError

    def get_volume(self) -> VolumeState:
        raise NotImplementedError

    def set_volume(self, level: Optional[int], mute: Optional[bool]) -> VolumeState:
        raise NotImplementedError

    def media_key(self, action: str) -> None:
        raise NotImplementedError

    def capture_screen(self, path: str) -> tuple[int, int]:
        """Write a screenshot to `path`; return (width, height). Returns a size,
        never bytes — see `desktop_tools.TakeScreenshotTool` for why."""
        raise NotImplementedError

    def read_clipboard(self) -> str:
        raise NotImplementedError

    def write_clipboard(self, text: str) -> None:
        raise NotImplementedError


class UnsupportedDesktopController(DesktopController):
    """Every call fails cleanly, naming the platform. This exists so that
    importing this module on macOS/Linux works and the suite runs there — the
    tools degrade exactly as they do when a hub is not connected."""

    def __init__(self, reason: str = "") -> None:
        self.platform_name = platform.system() or "this platform"
        self._reason = reason or (
            f"Desktop control is not supported on {self.platform_name} yet — "
            "it currently requires Windows."
        )

    def _fail(self, *_args: Any, **_kwargs: Any):
        raise DesktopError(self._reason)

    list_windows = _fail          # type: ignore[assignment]
    window = _fail                # type: ignore[assignment]
    focus_window = _fail          # type: ignore[assignment]
    close_window = _fail          # type: ignore[assignment]
    launch = _fail                # type: ignore[assignment]
    get_volume = _fail            # type: ignore[assignment]
    set_volume = _fail            # type: ignore[assignment]
    media_key = _fail             # type: ignore[assignment]
    capture_screen = _fail        # type: ignore[assignment]
    read_clipboard = _fail        # type: ignore[assignment]
    write_clipboard = _fail       # type: ignore[assignment]


# ============================================================ Windows / ctypes

# Virtual-key codes for the media keys (winuser.h). `keybd_event` is the
# simplest correct way to reach them: media keys are consumed by whichever app
# owns playback, which is precisely the "works for any player" behaviour wanted.
_VK = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
}
_KEYEVENTF_KEYUP = 0x0002

_SW_RESTORE = 9
_WM_CLOSE = 0x0010

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _declare_win32(user32: Any, kernel32: Any) -> None:
    """Declare restype/argtypes for every Win32 function this module calls.

    ⚠️ NOT BOILERPLATE — this is a correctness requirement on 64-bit Windows,
    and skipping it segfaulted the process during the live probe (2026-08-04).
    ctypes defaults an undeclared `restype` to `c_int`, which is 32 bits; a
    function returning a HANDLE or a pointer therefore has its result TRUNCATED,
    and the truncated value is then passed on as an address. `GetClipboardData`
    crashed outright. `OpenProcess` did NOT — it "worked" only because Windows
    happens to hand out small handle values, i.e. it was a latent crash waiting
    for a busier machine.

    Declaring every signature kills the whole bug class rather than the one that
    happened to fire. Nothing below is a guess: each mirrors the documented
    prototype in winuser.h / winbase.h.
    """
    from ctypes import POINTER, c_int, c_size_t, c_void_p
    from ctypes.wintypes import (
        BOOL, DWORD, HANDLE, HWND, INT, LPARAM, LPDWORD, LPWSTR, UINT, WPARAM,
    )

    # --- windows -----------------------------------------------------------
    user32.IsWindow.restype = BOOL
    user32.IsWindow.argtypes = [HWND]
    user32.IsWindowVisible.restype = BOOL
    user32.IsWindowVisible.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = INT
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextW.restype = INT
    user32.GetWindowTextW.argtypes = [HWND, LPWSTR, INT]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.GetWindowThreadProcessId.argtypes = [HWND, LPDWORD]
    user32.EnumWindows.restype = BOOL
    user32.EnumWindows.argtypes = [c_void_p, LPARAM]
    user32.ShowWindow.restype = BOOL
    user32.ShowWindow.argtypes = [HWND, INT]
    user32.SetForegroundWindow.restype = BOOL
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.PostMessageW.restype = BOOL
    user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]

    # --- clipboard (the one that crashed) ----------------------------------
    user32.OpenClipboard.restype = BOOL
    user32.OpenClipboard.argtypes = [HWND]
    user32.CloseClipboard.restype = BOOL
    user32.CloseClipboard.argtypes = []
    user32.EmptyClipboard.restype = BOOL
    user32.EmptyClipboard.argtypes = []
    user32.GetClipboardData.restype = HANDLE   # ← 64-bit; the truncation bug
    user32.GetClipboardData.argtypes = [UINT]
    user32.SetClipboardData.restype = HANDLE
    user32.SetClipboardData.argtypes = [UINT, HANDLE]

    # --- media keys --------------------------------------------------------
    user32.keybd_event.restype = None
    user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, DWORD, c_size_t]

    # --- processes ---------------------------------------------------------
    kernel32.OpenProcess.restype = HANDLE      # ← latent truncation
    kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    kernel32.CloseHandle.restype = BOOL
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.QueryFullProcessImageNameW.restype = BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [HANDLE, DWORD, LPWSTR, LPDWORD]

    # --- global memory (clipboard payloads) --------------------------------
    kernel32.GlobalAlloc.restype = HANDLE
    kernel32.GlobalAlloc.argtypes = [UINT, c_size_t]
    kernel32.GlobalLock.restype = c_void_p
    kernel32.GlobalLock.argtypes = [HANDLE]
    kernel32.GlobalUnlock.restype = BOOL
    kernel32.GlobalUnlock.argtypes = [HANDLE]
    _ = (POINTER, c_int)  # imported for the prototypes above; keep linters quiet


def _failed(hr: int) -> bool:
    """COM's own success test: the sign bit is the failure bit.

    A method that returns S_FALSE (1) — "no change was needed" — SUCCEEDED, and
    reading `hr != 0` as failure is the classic mistake. It cost a live probe
    here on 2026-08-04: muting an already-unmuted device raised
    "Could not change mute" while doing exactly what was asked."""
    return int(hr) < 0


class WindowsDesktopController(DesktopController):
    """Win32 via ctypes. No subprocess, no shell, no native module."""

    platform_name = "Windows"

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover — guarded by the factory
            raise DesktopError("WindowsDesktopController requires Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _declare_win32(self._user32, self._kernel32)

    # ---------------------------------------------------------- windows

    def list_windows(self) -> list[Window]:
        user32 = self._user32
        found: list[Window] = []

        # A visible top-level window with a non-empty title is what a person
        # means by "a window". Tool windows, the desktop shell and invisible
        # message-only windows all fail one of those tests.
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        def _callback(hwnd, _lparam):  # noqa: ANN001
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if not title:
                    return True
                found.append(
                    Window(
                        handle=int(hwnd or 0),
                        title=title,
                        process=self._process_name(hwnd),
                    )
                )
            except Exception:  # noqa: BLE001 — one bad window must not end the walk
                pass
            return len(found) < MAX_WINDOWS

        try:
            user32.EnumWindows(enum_proc(_callback), 0)
        except Exception as e:  # noqa: BLE001
            raise DesktopError(f"Could not list windows: {type(e).__name__}") from e
        return found

    def _process_name(self, hwnd: Any) -> str:
        """The executable name owning a window, or "". Best-effort: a protected
        process legitimately refuses, and a window we cannot name is still a
        window we can list."""
        try:
            pid = ctypes.c_uint(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return ""
            handle = self._kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if not handle:
                return ""
            try:
                size = ctypes.wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(size.value)
                if self._kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    return os.path.basename(buf.value)
            finally:
                self._kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def window(self, handle: int) -> Optional[Window]:
        try:
            hwnd = int(handle)
            if not self._user32.IsWindow(hwnd):
                return None
            length = self._user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(max(length, 0) + 1)
            self._user32.GetWindowTextW(hwnd, buf, max(length, 0) + 1)
            return Window(
                handle=int(handle),
                title=buf.value.strip(),
                process=self._process_name(hwnd),
            )
        except Exception:  # noqa: BLE001
            return None

    def focus_window(self, handle: int) -> None:
        try:
            hwnd = int(handle)
            # A minimized window must be restored first or SetForegroundWindow
            # brings a zero-size window forward and the user sees nothing.
            self._user32.ShowWindow(hwnd, _SW_RESTORE)
            self._user32.SetForegroundWindow(hwnd)
        except Exception as e:  # noqa: BLE001
            raise DesktopError(f"Could not focus that window: {type(e).__name__}") from e

    def close_window(self, handle: int) -> None:
        """POST a close REQUEST — deliberately not a kill.

        `WM_CLOSE` is what clicking the X sends: the application decides what to
        do, so an editor with unsaved work shows its save prompt instead of
        losing it. `TerminateProcess` would be a data-loss tool wearing a window
        tool's name."""
        try:
            hwnd = int(handle)
            if not self._user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0):
                raise DesktopError("The window did not accept the close request.")
        except DesktopError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DesktopError(f"Could not close that window: {type(e).__name__}") from e

    # ---------------------------------------------------------- launching

    def launch(self, path: str) -> None:
        """ShellExecute on a path discovered from the Start Menu.

        `os.startfile` IS ShellExecute, and it takes a PATH — there is no command
        line to build and therefore nothing for a quoting mistake or a model to
        inject into. Arguments are not supported on purpose."""
        try:
            os.startfile(path)  # noqa: S606 — a registry-resolved path, never a command
        except OSError as e:
            raise DesktopError(f"Could not start that application: {e}") from e

    # ---------------------------------------------------------- volume

    def _endpoint_volume(self):
        """An IAudioEndpointVolume for the default playback device.

        Hand-rolled COM over ctypes rather than a new dependency. The vtable
        indices below are the documented ordering of the interfaces (stable
        since Vista); every HRESULT is checked, so a mismatch surfaces as a
        DesktopError rather than a bad call into a wrong slot.

        ⚠️ SUCCESS IS `hr >= 0`, NOT `hr == 0` — see `_failed()`. Measured
        2026-08-04: `SetMute` returns S_FALSE (1) when the requested state is
        already the current one, and an `hr != 0` test read "mute an
        already-unmuted device" as a hard failure. `c_long` is used as the
        restype rather than `ctypes.HRESULT` deliberately: HRESULT makes ctypes
        raise its own OSError on a failing call, which would bypass every
        message below and surface as a bare Windows error code."""
        from ctypes import POINTER, byref, c_float, c_long, c_void_p
        from ctypes.wintypes import BOOL, DWORD

        ole32 = ctypes.WinDLL("ole32", use_last_error=True)

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def guid(text: str) -> _GUID:
            g = _GUID()
            if ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(g)) != 0:
                raise DesktopError("Could not read the audio device interface id.")
            return g

        CLSID_MMDeviceEnumerator = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
        IID_IMMDeviceEnumerator = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        IID_IAudioEndpointVolume = guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")

        # CoInitializeEx: S_OK(0) and S_FALSE(1) both mean "usable on this
        # thread"; RPC_E_CHANGED_MODE (0x80010106) means someone already
        # initialised it differently, which is still usable for our purposes.
        #
        # ⚠️ CALLED EVERY TIME ON PURPOSE, AND DELIBERATELY NOT PAIRED WITH
        # CoUninitialize. COM is initialised PER THREAD, and these tools can run
        # on the event loop or on an executor thread, so a module-level
        # "already done" flag would SKIP initialisation on a new thread and
        # every call there would fail — the tidy-looking optimisation is the
        # bug. A repeat call on an initialised thread just returns S_FALSE and
        # bumps a refcount, which is not a resource; uninitialising while an
        # interface may still be alive would be far worse.
        hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        if hr not in (0, 1, -2147417850):
            raise DesktopError("Could not initialise the audio subsystem.")

        enumerator = c_void_p()
        if ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator), None, 23,  # CLSCTX_ALL
            byref(IID_IMMDeviceEnumerator), byref(enumerator),
        ) != 0:
            raise DesktopError("No audio device enumerator on this machine.")

        def call(iface, index, restype, *argtypes):
            vtable = ctypes.cast(iface, POINTER(POINTER(c_void_p)))[0]
            proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
            return proto(vtable[index]), iface

        try:
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint is vtable slot 4
            # (0-2 IUnknown, 3 EnumAudioEndpoints).
            device = c_void_p()
            fn, this = call(enumerator, 4, c_long, c_long, c_long, POINTER(c_void_p))
            if _failed(fn(this, 0, 0, byref(device))):  # eRender, eConsole
                raise DesktopError("No default playback device.")
            try:
                # IMMDevice::Activate is slot 3.
                endpoint = c_void_p()
                fn, this = call(
                    device, 3, c_long,
                    POINTER(_GUID), DWORD, c_void_p, POINTER(c_void_p),
                )
                if _failed(fn(this, byref(IID_IAudioEndpointVolume), 23, None, byref(endpoint))):
                    raise DesktopError("Could not open the volume control.")
            finally:
                self._release(device)
        finally:
            self._release(enumerator)

        # IAudioEndpointVolume vtable (0-2 IUnknown, 3-4 notify (un)register,
        # 5 GetChannelCount, 6 SetMasterVolumeLevel, 7 SetMasterVolumeLevelScalar,
        # 8 GetMasterVolumeLevel, 9 GetMasterVolumeLevelScalar, ... 14 SetMute,
        # 15 GetMute).
        get_scalar, _ = call(endpoint, 9, c_long, POINTER(c_float))
        set_scalar, _ = call(endpoint, 7, c_long, c_float, POINTER(_GUID))
        get_mute, _ = call(endpoint, 15, c_long, POINTER(BOOL))
        set_mute, _ = call(endpoint, 14, c_long, BOOL, POINTER(_GUID))
        return endpoint, get_scalar, set_scalar, get_mute, set_mute

    def _release(self, iface) -> None:
        """IUnknown::Release — vtable slot 2. Best-effort; a leaked interface on
        an error path is far better than an exception during cleanup."""
        try:
            if not iface:
                return
            vtable = ctypes.cast(
                iface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            )[0]
            proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
            proto(vtable[2])(iface)
        except Exception:  # noqa: BLE001
            pass

    def get_volume(self) -> VolumeState:
        from ctypes import byref, c_float
        from ctypes.wintypes import BOOL

        endpoint, get_scalar, _set_scalar, get_mute, _set_mute = self._endpoint_volume()
        try:
            scalar = c_float(0.0)
            muted = BOOL(0)
            if _failed(get_scalar(endpoint, byref(scalar))) or _failed(
                get_mute(endpoint, byref(muted))
            ):
                raise DesktopError("Could not read the current volume.")
            return VolumeState(level=int(round(scalar.value * 100)), muted=bool(muted.value))
        finally:
            self._release(endpoint)

    def set_volume(self, level: Optional[int], mute: Optional[bool]) -> VolumeState:
        from ctypes import byref, c_float
        from ctypes.wintypes import BOOL

        endpoint, get_scalar, set_scalar, get_mute, set_mute = self._endpoint_volume()
        try:
            if mute is not None:
                # S_FALSE (1) here means "already in that state" — a success.
                if _failed(set_mute(endpoint, BOOL(1 if mute else 0), None)):
                    raise DesktopError("Could not change mute.")
            if level is not None:
                if _failed(set_scalar(endpoint, c_float(max(0, min(100, level)) / 100.0), None)):
                    raise DesktopError("Could not change the volume.")
            scalar = c_float(0.0)
            muted = BOOL(0)
            get_scalar(endpoint, byref(scalar))
            get_mute(endpoint, byref(muted))
            return VolumeState(level=int(round(scalar.value * 100)), muted=bool(muted.value))
        finally:
            self._release(endpoint)

    def media_key(self, action: str) -> None:
        code = _VK.get(action)
        if code is None:
            raise DesktopError(f"'{action}' is not a media key.")
        try:
            self._user32.keybd_event(code, 0, 0, 0)
            self._user32.keybd_event(code, 0, _KEYEVENTF_KEYUP, 0)
        except Exception as e:  # noqa: BLE001
            raise DesktopError(f"Could not send that media key: {type(e).__name__}") from e

    # ---------------------------------------------------------- screen

    def capture_screen(self, path: str) -> tuple[int, int]:
        try:
            from PIL import ImageGrab
        except ImportError as e:
            raise DesktopError(
                "Screenshots need the Pillow library, which is not installed."
            ) from e
        try:
            # all_screens: a multi-monitor setup should capture what is actually
            # on the desk, not just the primary display.
            image = ImageGrab.grab(all_screens=True)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            image.save(path, format="PNG")
            return image.width, image.height
        except Exception as e:  # noqa: BLE001
            raise DesktopError(f"Could not capture the screen: {type(e).__name__}") from e

    # ---------------------------------------------------------- clipboard

    def read_clipboard(self) -> str:
        user32, kernel32 = self._user32, self._kernel32
        if not user32.OpenClipboard(None):
            raise DesktopError("Could not open the clipboard — another app has it.")
        try:
            handle = user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""  # non-text clipboard (an image, files) reads as empty
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.c_wchar_p(pointer).value or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def write_clipboard(self, text: str) -> None:
        user32, kernel32 = self._user32, self._kernel32
        data = str(text)
        size = (len(data) + 1) * ctypes.sizeof(ctypes.c_wchar)
        if not user32.OpenClipboard(None):
            raise DesktopError("Could not open the clipboard — another app has it.")
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
            if not handle:
                raise DesktopError("Could not allocate clipboard memory.")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise DesktopError("Could not lock clipboard memory.")
            try:
                ctypes.memmove(pointer, ctypes.create_unicode_buffer(data), size)
            finally:
                kernel32.GlobalUnlock(handle)
            # Ownership of `handle` passes to the clipboard on success — it must
            # NOT be freed here. On failure the block below leaks one small
            # allocation, which is the right trade against a double free.
            if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
                raise DesktopError("The clipboard rejected the text.")
        finally:
            user32.CloseClipboard()


# ============================================================ the factory

#: Injectable seam. Tests swap this; production leaves it None. Resolved at CALL
#: time, never import time, so a fixture installed after import still wins.
DESKTOP_CONTROLLER_FACTORY: Optional[Callable[[], DesktopController]] = None

_controller: Optional[DesktopController] = None


def reset_desktop_controller() -> None:
    """Drop the cached controller (tests, and a settings change)."""
    global _controller
    _controller = None


def get_controller() -> DesktopController:
    """The controller for this machine. Cached — building one is cheap, but the
    Windows implementation loads two DLLs and there is no reason to repeat it."""
    global _controller
    if DESKTOP_CONTROLLER_FACTORY is not None:
        # Never cached: a test's factory may want to answer differently per call.
        return DESKTOP_CONTROLLER_FACTORY()
    if _controller is None:
        if sys.platform == "win32":
            try:
                _controller = WindowsDesktopController()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Desktop control unavailable: {type(e).__name__}: {e}")
                _controller = UnsupportedDesktopController(
                    f"Desktop control could not start on this machine: {e}"
                )
        else:
            _controller = UnsupportedDesktopController()
    return _controller


# ============================================================ the app registry

_START_MENU_SUBPATH = Path("Microsoft/Windows/Start Menu/Programs")

#: Shortcuts that are not "applications a person launches by name". Uninstallers
#: and web-link shortcuts are the two that would otherwise be reachable — the
#: first is destructive, the second opens a browser at an arbitrary URL, which
#: is the browser stack's job and subject to its origin grounding.
_APP_NAME_EXCLUSIONS = (
    "uninstall", "uninstaller", "remove ", "readme", "read me",
    "license", "licence", "release notes", "documentation", "help",
    "website", "web site", "home page", "homepage",
)

_apps_cache: list[AppEntry] = []
_apps_cached_at: float = 0.0


def _start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    for env in ("ProgramData", "APPDATA"):
        root = os.environ.get(env, "")
        if root:
            candidate = Path(root) / _START_MENU_SUBPATH
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def _looks_like_an_app(stem: str) -> bool:
    low = stem.lower()
    return not any(marker in low for marker in _APP_NAME_EXCLUSIONS)


def discover_apps(*, force: bool = False) -> list[AppEntry]:
    """Every application this user can launch, from the Start Menu.

    A `.lnk` is used AS the launch target — `os.startfile` resolves the shortcut
    itself, so nothing here needs to read a shortcut's internals (which would
    mean COM) and nothing composes a command line.

    Best-effort by design: an unreadable directory yields fewer apps, never an
    error. An empty list is a legitimate answer (a fresh Windows install, or any
    non-Windows machine)."""
    global _apps_cache, _apps_cached_at
    if not force and _apps_cache and (time.monotonic() - _apps_cached_at) < APP_CACHE_SECONDS:
        return _apps_cache

    seen: dict[str, AppEntry] = {}
    for directory in _start_menu_dirs():
        try:
            for link in directory.rglob("*.lnk"):
                stem = link.stem.strip()
                if not stem or not _looks_like_an_app(stem):
                    continue
                key = stem.lower()
                # First writer wins: ProgramData (all users) is walked before
                # APPDATA, and a duplicate name is the same app either way.
                if key not in seen:
                    seen[key] = AppEntry(name=stem, path=str(link))
                if len(seen) >= MAX_APPS:
                    break
        except OSError:
            continue

    _apps_cache = sorted(seen.values(), key=lambda a: a.name.lower())
    _apps_cached_at = time.monotonic()
    return _apps_cache


@dataclass(frozen=True)
class AppMatch:
    """The outcome of resolving a spoken app name against the registry.

    Exactly one of these is true: `entry` is set (resolved), `candidates` has
    two or more (ambiguous — the plan must ASK), or both are empty (no match).
    """

    entry: Optional[AppEntry] = None
    candidates: tuple[AppEntry, ...] = ()


def resolve_app(name: str, apps: Optional[list[AppEntry]] = None) -> AppMatch:
    """A name a person said → an installed application, or an honest non-answer.

    Uses the memory engine's identity-resolution convention (`MIN_SCORE = 81`,
    `MIN_GAP = 8`) so app matching behaves like contact matching: a weak best
    match is NO match, and a best match too close to the runner-up is AMBIGUOUS
    rather than a coin flip between "Code" and "Code - Insiders".
    """
    from rapidfuzz import fuzz

    wanted = str(name or "").strip()
    if not wanted:
        return AppMatch()
    registry = apps if apps is not None else discover_apps()
    if not registry:
        return AppMatch()

    low = wanted.lower()
    # An exact (case-insensitive) name is never ambiguous — if the user says
    # "Code" and an app is literally called "Code", that is the one.
    for app in registry:
        if app.name.lower() == low:
            return AppMatch(entry=app)

    scored = sorted(
        ((max(fuzz.WRatio(low, a.name.lower()), fuzz.partial_ratio(low, a.name.lower())), a)
         for a in registry),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < APP_MIN_SCORE:
        return AppMatch()
    rivals = [a for score, a in scored[1:] if best_score - score < APP_MIN_GAP]
    if rivals:
        return AppMatch(candidates=tuple([best, *rivals[:4]]))
    return AppMatch(entry=best)


def closest_app_names(name: str, limit: int = 4) -> list[str]:
    """The nearest installed app names to a name that did not resolve — so a
    failure can say "did you mean…" instead of just "no"."""
    from rapidfuzz import fuzz

    registry = discover_apps()
    if not registry or not str(name or "").strip():
        return [a.name for a in registry[:limit]]
    low = str(name).strip().lower()
    ranked = sorted(
        registry, key=lambda a: fuzz.WRatio(low, a.name.lower()), reverse=True
    )
    return [a.name for a in ranked[:limit]]


# ============================================================ screenshots

def screenshot_dir() -> Path:
    """`~/.jarvis/screenshots` — outside the repo, beside the trash and the
    tokens (`file_tools.TRASH_DIR`'s convention)."""
    return Path.home() / ".jarvis" / SCREENSHOT_DIR_NAME


def new_screenshot_path() -> Path:
    return screenshot_dir() / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"


async def sweep_screenshots(db: Any) -> None:
    """Delete screenshots older than the configured retention.

    ⚠️ THE ONE PLACE IN THIS FEATURE THAT DELETES ANYTHING, and it is why
    `take_screenshot` returning a path is acceptable at all: an image of the
    user's entire screen must not accumulate on disk indefinitely just because
    they once asked for a screenshot. It only ever touches `screen-*.png` files
    inside `~/.jarvis/screenshots` — a file a user deliberately saved elsewhere
    is not in scope, and neither is anything they renamed.

    Best-effort throughout (the housekeeping-step rule): a locked or vanished
    file is skipped, never raised. Takes `db` because every housekeeping step
    does; it reads the retention setting from it."""
    from app.core.app_settings import get_desktop_config

    config = await get_desktop_config(db)
    directory = screenshot_dir()
    if not directory.is_dir():
        return
    cutoff = time.time() - (config.screenshot_retention_days * 86400)
    removed = 0
    for path in directory.glob("screen-*.png"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info(f"Housekeeping: removed {removed} screenshot(s) past retention")
