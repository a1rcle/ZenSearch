"""
ZenSearch - Low-Level Keyboard Hook & Hotkey State Machine
Implements:
1. Double-Ctrl detection (250~350ms window with hotkey conflict prevention)
2. Ctrl + G Quick Switch detection
3. Explorer Type-to-Search key interception
"""

import time
import threading
import ctypes
from ctypes import wintypes
from typing import Callable, Optional
import win32gui
import win32con
from core.explorer import ExplorerManager
from core.injector import PathInjector

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_CONTROL = 0x11
VK_G = 0x47

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('vkCode', wintypes.DWORD),
        ('scanCode', wintypes.DWORD),
        ('flags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ctypes.c_ulonglong)
    ]

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]

user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p]

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]

user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

user32.GetMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

user32.DispatchMessageW.restype = ctypes.c_longlong
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = []

kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]



class HotkeyManager:
    def __init__(self,
                 on_double_ctrl: Optional[Callable[[], None]] = None,
                 on_quick_switch: Optional[Callable[[], None]] = None,
                 on_explorer_type: Optional[Callable[[str], None]] = None,
                 on_auto_switch: Optional[Callable[[int], None]] = None):
        self.on_double_ctrl = on_double_ctrl
        self.on_quick_switch = on_quick_switch
        self.on_explorer_type = on_explorer_type
        self.on_auto_switch = on_auto_switch

        self.explorer_mgr = ExplorerManager.get_instance()
        self.injector = PathInjector()

        self._hook_thread: Optional[threading.Thread] = None
        self._hook_id = None
        self._hook_proc = None
        self._thread_id = None
        self._running = False

        # Double Ctrl state machine
        self._last_ctrl_time = 0.0
        self._ctrl_press_count = 0

    def start(self):
        if self._running:
            return
        self._running = True
        self.explorer_mgr.start_tracking(auto_switch=True, on_auto_switch=self.on_auto_switch)
        self._hook_thread = threading.Thread(target=self._run_hook_loop, daemon=True)
        self._hook_thread.start()

    def stop(self):
        self._running = False
        self.explorer_mgr.stop_tracking()
        if self._hook_id:
            user32.UnhookWindowsHookEx(self._hook_id)
            self._hook_id = None
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, win32con.WM_QUIT, 0, 0)

    def _ensure_desktop(self):
        try:
            h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
            if h_desk:
                user32.SetThreadDesktop(h_desk)
        except Exception:
            pass

    def _is_ctrl_down(self) -> bool:
        return bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)

    def _is_alt_down(self) -> bool:
        return bool(user32.GetAsyncKeyState(win32con.VK_MENU) & 0x8000)

    def _is_in_edit_or_rename(self, hwnd: int) -> bool:
        """Checks if current focus inside Explorer is an edit/rename box."""
        try:
            class GUITHREADINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', wintypes.DWORD),
                    ('flags', wintypes.DWORD),
                    ('hwndActive', wintypes.HWND),
                    ('hwndFocus', wintypes.HWND),
                    ('hwndCapture', wintypes.HWND),
                    ('hwndMenuOwner', wintypes.HWND),
                    ('hwndMoveSize', wintypes.HWND),
                    ('hwndCaret', wintypes.HWND),
                    ('rcCaret', wintypes.RECT)
                ]
            gti = GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(GUITHREADINFO)
            tid, _ = win32gui.GetWindowThreadProcessId(hwnd)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                if gti.hwndFocus:
                    cls_name = win32gui.GetClassName(gti.hwndFocus)
                    # If focused on an Edit, Address bar, or search box, don't intercept
                    if 'Edit' in cls_name or 'Search' in cls_name or 'Breadcrumb' in cls_name:
                        return True
        except Exception:
            pass
        return False

    def _hook_callback(self, nCode, wParam, lParam):
        if nCode >= 0:
            kb = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = kb.vkCode

            # 1. Double Ctrl state machine
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if vk in (VK_LCONTROL, VK_RCONTROL, VK_CONTROL):
                    now = time.time()
                    elapsed = now - self._last_ctrl_time
                    if 0.05 < elapsed < 0.35:
                        self._last_ctrl_time = 0.0
                        if self.on_double_ctrl:
                            # Fire double ctrl callback on separate thread
                            threading.Thread(target=self.on_double_ctrl, daemon=True).start()
                    else:
                        self._last_ctrl_time = now
                else:
                    # Non-Ctrl key pressed, reset double-ctrl counter
                    self._last_ctrl_time = 0.0

                    # 2. Ctrl + G Quick Switch in Common File Dialog
                    if vk == VK_G and self._is_ctrl_down() and not self._is_alt_down():
                        fg_hwnd = win32gui.GetForegroundWindow()
                        dlg = self.explorer_mgr.find_active_dialog(fg_hwnd)
                        if dlg:
                            if self.on_quick_switch:
                                threading.Thread(target=self.on_quick_switch, daemon=True).start()
                            else:
                                threading.Thread(target=self.injector.quick_switch, daemon=True).start()
                            return 1 # Suppress G in dialog

                    # 3. Explorer Type-to-Search
                    if not self._is_ctrl_down() and not self._is_alt_down():
                        fg_hwnd = win32gui.GetForegroundWindow()
                        if self.explorer_mgr.is_explorer_window(fg_hwnd):
                            # Ensure not in rename edit box or address bar
                            if not self._is_in_edit_or_rename(fg_hwnd):
                                # Printable character check: A-Z, 0-9
                                if (0x30 <= vk <= 0x39) or (0x41 <= vk <= 0x5A) or vk == win32con.VK_SPACE:
                                    char = chr(vk).lower() if (0x41 <= vk <= 0x5A) else chr(vk)
                                    if self.on_explorer_type:
                                        threading.Thread(target=self.on_explorer_type, args=(char,), daemon=True).start()
                                        return 1 # Absorb character into companion search

        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run_hook_loop(self):
        self._ensure_desktop()
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook_proc = HOOKPROC(self._hook_callback)
        self._hook_id = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._hook_proc,
            kernel32.GetModuleHandleW(None),
            0
        )

        msg = wintypes.MSG()
        while self._running:
            b_ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if b_ret == 0 or b_ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook_id:
            user32.UnhookWindowsHookEx(self._hook_id)
            self._hook_id = None
