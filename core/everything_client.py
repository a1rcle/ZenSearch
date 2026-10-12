"""
ZenSearch - Everything IPC Search Client
Implements direct Windows IPC communication with any running Everything instance
via WM_COPYDATA protocol (Everything IPC version 1 / 2).
"""

import os
import struct
import threading
import time
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional

import win32gui
import win32con

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]


EVERYTHING_IPC_COPYDATAQUERYW = 2

# EVERYTHING_IPC_ITEM flags
EVERYTHING_IPC_FOLDER = 0x00000001
EVERYTHING_IPC_DRIVE = 0x00000002
EVERYTHING_IPC_ROOT = 0x00000002

class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [
        ('dwData', wintypes.LPARAM),
        ('cbData', wintypes.DWORD),
        ('lpData', ctypes.c_void_p)
    ]

@dataclass
class SearchResult:
    name: str
    path: str
    full_path: str
    is_folder: bool


class EverythingClient:
    _instance: Optional['EverythingClient'] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self.reply_hwnd = None
        self._atom = None
        self._target_hwnd = None
        self._query_lock = threading.Lock()
        self._query_event = threading.Event()
        self._current_results: List[SearchResult] = []
        self._seq = 100
        self._running = True

        self._ensure_desktop()
        self._init_reply_window()

    @classmethod
    def get_instance(cls) -> 'EverythingClient':
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = EverythingClient()
            return cls._instance

    def _ensure_desktop(self):
        """Ensure current thread is connected to user's interactive Default desktop."""
        try:
            h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
            if h_desk:
                user32.SetThreadDesktop(h_desk)
        except Exception:
            pass

    def _init_reply_window(self):
        """Creates a hidden native window to receive WM_COPYDATA reply from Everything."""
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == win32con.WM_COPYDATA:
                try:
                    cds = COPYDATASTRUCT.from_address(lparam)
                    raw_bytes = ctypes.string_at(cds.lpData, cds.cbData)
                    if len(raw_bytes) >= 28:
                        totf, totfi, tot, numf, numfi, num, off = struct.unpack_from('<7I', raw_bytes, 0)
                        items = []
                        for i in range(num):
                            flags, fn_off, p_off = struct.unpack_from('<3I', raw_bytes, 28 + i * 12)
                            fn = ctypes.wstring_at(cds.lpData + fn_off)
                            p = ctypes.wstring_at(cds.lpData + p_off)
                            if p:
                                if p.endswith(':'):
                                    full_path = p + '\\' + fn
                                else:
                                    full_path = os.path.join(p, fn)
                            else:
                                full_path = fn
                            is_folder = bool(flags & (EVERYTHING_IPC_FOLDER | EVERYTHING_IPC_DRIVE | EVERYTHING_IPC_ROOT))
                            items.append(SearchResult(name=fn, path=p, full_path=full_path, is_folder=is_folder))
                        self._current_results = items
                except Exception as e:
                    pass
                finally:
                    self._query_event.set()
                return 1
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = wnd_proc
        wc.lpszClassName = f'ZenSearch_IPC_ReplyWindow_{os.getpid()}'
        try:
            self._atom = win32gui.RegisterClass(wc)
            self.reply_hwnd = win32gui.CreateWindow(
                self._atom,
                'ZenSearchReplyWnd',
                0, 0, 0, 0, 0, 0, 0, 0, None
            )
        except Exception as e:
            pass

    def find_target_window(self) -> Optional[int]:
        """Finds any running Everything notification window."""
        self._ensure_desktop()
        # 1. Try standard EVERYTHING_TASKBAR_NOTIFICATION
        hwnd = win32gui.FindWindow('EVERYTHING_TASKBAR_NOTIFICATION', None)
        if hwnd and win32gui.IsWindow(hwnd):
            self._target_hwnd = hwnd
            return hwnd

        # 2. Try any instance-suffixed or main EVERYTHING window
        candidates = []
        def enum_cb(h, _):
            try:
                c = win32gui.GetClassName(h)
                if 'EVERYTHING' in c.upper():
                    candidates.append(h)
            except Exception:
                pass

        try:
            h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
            if h_desk:
                win32gui.EnumDesktopWindows(h_desk, enum_cb, None)
            else:
                win32gui.EnumWindows(enum_cb, None)
        except Exception:
            try:
                win32gui.EnumWindows(enum_cb, None)
            except Exception:
                pass

        if candidates:
            self._target_hwnd = candidates[0]
            return candidates[0]

        # 3. Auto-launch fallback via EverythingProbe if installed but not running
        from core.everything_probe import EverythingProbe
        ready, _ = EverythingProbe.ensure_everything_running()
        if ready:
            hwnd = win32gui.FindWindow('EVERYTHING_TASKBAR_NOTIFICATION', None)
            if hwnd and win32gui.IsWindow(hwnd):
                self._target_hwnd = hwnd
                return hwnd

        return None

    def is_available(self) -> bool:
        """Returns True if Everything service/process is reachable."""
        return self.find_target_window() is not None

    def search(self, query: str, max_results: int = 50) -> List[SearchResult]:
        """
        Executes a search query against Everything.
        Returns a list of SearchResult objects (up to max_results).
        """
        if not query or not query.strip():
            return []

        target = self.find_target_window()
        if not target or not self.reply_hwnd:
            return []

        with self._query_lock:
            self._current_results = []
            self._query_event.clear()
            self._seq += 1

            search_bytes = query.encode('utf-16le') + b'\x00\x00'
            # Struct EVERYTHING_IPC_QUERYW:
            # reply_hwnd(DWORD), reply_copydata_message(DWORD), search_flags(DWORD), offset(DWORD), max_results(DWORD), search_string(WCHAR[...])
            query_header = struct.pack('<5I', self.reply_hwnd, self._seq, 0, 0, max_results)
            query_payload = query_header + search_bytes

            cds = COPYDATASTRUCT()
            cds.dwData = EVERYTHING_IPC_COPYDATAQUERYW
            cds.cbData = len(query_payload)
            cds.lpData = ctypes.cast(ctypes.c_char_p(query_payload), ctypes.c_void_p)

            # Send query to Everything
            ret = user32.SendMessageW(target, win32con.WM_COPYDATA, self.reply_hwnd, ctypes.byref(cds))
            if not ret:
                return []

            # Pump waiting messages until result arrives or timeout (max 400ms)
            start_t = time.time()
            while time.time() - start_t < 0.4:
                win32gui.PumpWaitingMessages()
                if self._query_event.is_set():
                    break
                time.sleep(0.005)

            return list(self._current_results)

    def search_scoped(self, folder_path: str, query: str, max_results: int = 50) -> List[SearchResult]:
        """
        Performs a scoped search restricted to folder_path and its subdirectories.
        """
        clean_folder = folder_path.rstrip('\\/')
        if not query or not query.strip():
            # If query is empty, show contents inside folder
            full_query = f'path:"{clean_folder}"'
        else:
            full_query = f'path:"{clean_folder}" {query.strip()}'
        return self.search(full_query, max_results=max_results)

    def close(self):
        self._running = False
        if self.reply_hwnd:
            try:
                win32gui.DestroyWindow(self.reply_hwnd)
            except Exception:
                pass
            self.reply_hwnd = None
        if self._atom:
            try:
                win32gui.UnregisterClass(self._atom, None)
            except Exception:
                pass
            self._atom = None
