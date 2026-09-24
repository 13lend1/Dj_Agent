"""System-wide DJ hotkeys that work even when the browser tab is in the
background.

Browsers only deliver key events to the focused tab, so the in-page Ctrl+Alt
shortcuts silently die the moment the DJ page loses focus. This daemon
registers GLOBAL Windows hotkeys (Ctrl+Alt+N = skip, Ctrl+Alt+L = like,
Ctrl+Alt+F = full, Ctrl+Alt+P = previous, Ctrl+Alt+D = dislike,
Ctrl+Alt+Space = pause/resume) and forwards
them to the DJ's HTTP API, so the deck responds no matter what window has
focus. Each press also pops a standard Windows tray notification
(Skipping, Liked, ...) so you get visual feedback while another app has focus.

Run it alongside the server (any terminal window):
    python hotkeys.py                 # assumes server on 127.0.0.1:8000
    python hotkeys.py --port 8080
    python hotkeys.py --host 127.0.0.1 --port 8000

The API server (api/server.py) also starts this automatically in a background
thread — use --no-hotkeys there to disable it.

Stdlib only (ctypes + urllib): no pynput/keyboard/requests needed, no admin.
"""

import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from ctypes import windll

kernel32 = windll.kernel32
user32 = windll.user32
shell32 = windll.shell32

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010

NIIF_NONE = 0x00000000
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003
NIIF_NOSOUND = 0x00000010

WM_USER = 0x0400
WM_TRAY_CALLBACK = WM_USER + 20
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
PM_NOREMOVE = 0x0000

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
WM_HOTKEY = 0x0312

VK_N = 0x4E
VK_L = 0x4C
VK_F = 0x46
VK_P = 0x50
VK_D = 0x44
VK_SPACE = 0x20

# (registered id, virtual key, label, control url, optional json body)
HOTKEYS = [
    (1, VK_N, "Ctrl+Alt+N skip", "/skip", None),
    (2, VK_L, "Ctrl+Alt+L like", "/rate", {"rating": 0}),
    (3, VK_F, "Ctrl+Alt+F full", "/full", None),
    (4, VK_P, "Ctrl+Alt+P previous", "/previous", None),
    (5, VK_D, "Ctrl+Alt+D dislike", "/rate", {"rating": 1}),
    (6, VK_SPACE, "Ctrl+Alt+Space pause", "/pause", None),
]

hotkey_labels = {ht_id: label for ht_id, _, label, _, _ in HOTKEYS}


class _NOTIFYICONDATAW(ctypes.Structure):
    # Only the fields we use; order must stay native.
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

# A normal application icon for the tray slot (shared system icon, do not delete).
IDI_APPLICATION = 32512
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HICON
_TRAY_HICON = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)


@_WNDPROC
def _tray_wndproc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, wintypes.INT, wintypes.INT,
    wintypes.INT, wintypes.INT, wintypes.HWND,
    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


# ---- tray notification state ---------------------------------------------
# Each notification gets its OWN fresh tray icon (uID via an incrementing
# counter). This "NIM_ADD a brand-new icon per popup" trick is what makes
# balloons actually appear: Explorer throttles balloons on the SAME icon for
# ~30s, but a new icon every time slips right past that. We delete the icon a
# few seconds later once Explorer has grabbed the balloon.

_tray_hwnd = None
_tray_class_registered = False
_tray_next_uid = 0
_tray_lock = threading.Lock()
_tray_queue = queue.Queue()
_tray_keep_ms = 1500
_tray_lifetime_ms = 12000
_tray_window_owner_created = False


def _ensure_tray_window():
    """Create (once) a hidden window that owns the tray icons."""
    global _tray_hwnd, _tray_class_registered
    if _tray_hwnd is not None:
        return _tray_hwnd
    with _tray_lock:
        if _tray_hwnd is not None:
            return _tray_hwnd
        if not _tray_class_registered:
            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.style = 0
            wc.lpfnWndProc = _tray_wndproc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.hbrBackground = None
            wc.lpszClassName = "dj_agent_hotkeys"
            if user32.RegisterClassExW(ctypes.byref(wc)):
                _tray_class_registered = True
        if not _tray_class_registered:
            return None
        hwnd = user32.CreateWindowExW(
            0, "dj_agent_hotkeys", "DJ Agent Hotkeys",
            0, 0, 0, 0, 0, None, None,
            kernel32.GetModuleHandleW(None), None,
        )
        if hwnd:
            _tray_hwnd = hwnd
        return _tray_hwnd


def _tray_worker():
    """Deliver queued balloons one at a time."""
    global _tray_next_uid
    while True:
        item = _tray_queue.get()
        if item is None:
            break
        title, message = item
        try:
            hwnd = _ensure_tray_window()
            if not hwnd:
                print(f"[notify] tray window unavailable for {message!r}",
                      file=sys.stderr)
                continue
            with _tray_lock:
                _tray_next_uid = (_tray_next_uid + 1) % 0x7FFFFFFF
                uid = _tray_next_uid

            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = uid
            nid.hIcon = _TRAY_HICON
            nid.uFlags = NIF_ICON | NIF_INFO | NIF_TIP
            nid.szInfoTitle = title[:63]
            nid.szInfo = message[:255]
            nid.uTimeout = 4000
            nid.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND
            nid.szTip = "DJ Agent hotkeys"
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            if not ok:
                print(f"[notify] Shell_NotifyIconW failed: {message!r}",
                      file=sys.stderr)
                continue
            print(f"[{time.strftime('%H:%M:%S')}] tray: {title} — {message}",
                  flush=True)
            # let Explorer host the balloon, then remove the icon
            time.sleep(_tray_lifetime_ms / 1000)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception as e:
            print(f"[notify] tray notify failed: {e}", file=sys.stderr)


def notify_tray(message, title="DJ Agent", timeout_ms=2500):
    """Queue a standard Windows tray balloon. Never raises."""
    if sys.platform != "win32":
        return
    global _tray_window_owner_created
    try:
        if not _tray_window_owner_created:
            t = threading.Thread(target=_tray_worker, name="dj-tray", daemon=True)
            t.start()
            _tray_window_owner_created = True
        # drop anything still queued so rapid hotkeys show the latest
        while True:
            try:
                _tray_queue.get_nowait()
            except queue.Empty:
                break
        _tray_queue.put((str(title), str(message)))
    except Exception as e:
        print(f"[notify] failed to queue: {e}", file=sys.stderr)


def cleanup_tray():
    """Remove any live tray icon and stop the worker. Safe to call anytime."""
    global _tray_hwnd, _tray_class_registered, _tray_window_owner_created
    try:
        _tray_queue.put(None)
    except Exception:
        pass
    if _tray_hwnd is not None:
        hwnd = _tray_hwnd
        _tray_hwnd = None
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
    _tray_class_registered = False
    _tray_window_owner_created = False


# Friendly status line for each hotkey.
_NOTIFY_BY_ACTION = {
    1: "Skipping \u2192 next song",
    2: "Liked \u2665",
    3: "Playing full \u2665",
    4: "Previous song",
    5: "Disliked \u2717",
}


def fire(ht_id, base_url):
    label = hotkey_labels.get(int(ht_id), f"hotkey {ht_id}")
    notify_message = _NOTIFY_BY_ACTION.get(int(ht_id))
    for _id, _vk_code, _label, path, body in HOTKEYS:
        if _id != int(ht_id):
            continue
        url = f"{base_url}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                try:
                    payload = json.loads(resp.read().decode("utf-8") or "{}")
                except Exception:
                    payload = {}
            print(f"[{time.strftime('%H:%M:%S')}] {label} -> {status}", flush=True)
            if int(ht_id) == 6:
                notify_tray(
                    "Paused \u23F8" if payload.get("paused") else "Resumed \u25B6"
                )
            elif notify_message:
                notify_tray(notify_message)
        except urllib.error.HTTPError as e:
            print(f"[{time.strftime('%H:%M:%S')}] {label} -> HTTP {e.code} "
                  f"(deck not running?)", flush=True)
            notify_tray(f"Deck not running (HTTP {e.code})")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] {label} -> {e}", flush=True)
            notify_tray(f"Could not reach the DJ ({type(e).__name__})")
        return


def run_hotkeys(host="127.0.0.1", port=8000, stop_event=None):
    """Register the global hotkeys and pump the message loop ON THIS THREAD.

    Used two ways:
      * standalone: python hotkeys.py  (message loop runs on the main thread)
      * in-process: api/server.py spawns this in a background daemon thread, so
        the shortcuts ride along with the server (still "in the background" —
        no extra process or window) and stop when the server exits.

    Returns the number of hotkeys actually registered (0 means nothing to do).
    The loop runs until `stop_event` is set or the process is terminating."""
    if sys.platform != "win32":
        print("Global hotkeys need Windows (RegisterHotKey). "
              "See pygame/pynput for other platforms.", file=sys.stderr)
        return 0
    if stop_event is None:
        stop_event = threading.Event()
    base_url = f"http://{host}:{port}/api/control"

    # A RegisterHotKey with a NULL window handle binds to the CALLING THREAD's
    # message queue. If that queue doesn't exist yet, Windows happily returns
    # success but never delivers the WM_HOTKEY — the classic "registers fine,
    # never fires anywhere" trap (and exactly how it showed up: hotkeys worked
    # in the focused tab but nowhere else). Pump one no-remove message first to
    # force the queue into existence, THEN register.
    msg = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

    registered = 0
    for ht_id, vk, label, _, _ in HOTKEYS:
        if user32.RegisterHotKey(None, ht_id, MOD_CONTROL | MOD_ALT, vk):
            registered += 1
            print(f"registered: {label}")
        else:
            print(f"WARNING: could not register {label} "
                  f"(in use by another app?)", file=sys.stderr)
    if not registered:
        print("No hotkeys registered — nothing to do.", file=sys.stderr)
        return 0

    print(f"listening for global hotkeys -> {base_url}", flush=True)
    try:
        while not stop_event.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    break
                if msg.message == WM_HOTKEY:
                    fire(msg.wParam, base_url)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.05)
    finally:
        for ht_id, _, _, _, _ in HOTKEYS:
            user32.UnregisterHotKey(None, ht_id)
        cleanup_tray()
    return registered


def start_hotkey_thread(host="127.0.0.1", port=8000):
    """Launch the global-hotkey listener as a daemon thread in THIS process
    (used by api/server.py). The thread dies with the server, and Windows frees
    the hotkey registrations automatically on exit — nothing to clean up.
    Returns the thread, or None on non-Windows."""
    if sys.platform != "win32":
        return None
    stop_event = threading.Event()

    def _run():
        try:
            run_hotkeys(host=host, port=port, stop_event=stop_event)
        except Exception as exc:
            print("hotkey daemon stopped:", exc, file=sys.stderr)

    thread = threading.Thread(target=_run, name="dj-hotkeys", daemon=True)
    thread.start()
    return thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    try:
        registered = run_hotkeys(args.host, args.port)
    except KeyboardInterrupt:
        print("\nstopping...", flush=True)
        return 0
    return 0 if registered else 1


if __name__ == "__main__":
    sys.exit(main())