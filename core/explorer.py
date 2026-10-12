"""
ZenSearch - Explorer Interop & Strict MRU Path Sniffer
Reference: ZenSearch技术方案.md, 路径注入.md

Implements:
1. Strict MRU Policy (严格 MRU 定向原则与零模糊兜底):
   - Tracks the most recently active Explorer window via WinEventHook (EVENT_SYSTEM_FOREGROUND).
   - Only navigates to the directory of the last actual browsed/activated Explorer window.
   - Refuses ambiguous fallbacks across arbitrary background Explorer windows.
   - Safely ignores/silences if no Explorer window was browsed or if the recorded window is closed.
2. Windows 11 Tabs & Single Window Multi-Tab Awareness:
   - Identifies active tab via visible ShellTabWindowClass and window title matching.
   - Correctly extracts the path of the active tab instead of background tabs.
3. Common Dialog (#32770) detection and optional auto-switch coordination.
"""

import os
import time
import threading
import urllib.parse
from typing import Optional, Tuple, Callable
import ctypes
from ctypes import wintypes

import win32gui
import win32con
import win32process
import win32com.client
import pythoncom

from core.logger import logger

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000

EXPLORER_CLASS_NAMES = {'CabinetWClass', 'ExploreWClass'}
DIALOG_CLASS_NAMES = {'#32770'}

# ctypes win32 signatures
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]

user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD
]

user32.UnhookWinEvent.restype = wintypes.BOOL
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

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

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD
)


def ensure_desktop():
    """Mount current thread to interactive 'Default' desktop."""
    try:
        h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
    except Exception:
        pass


class ExplorerManager:
    _instance: Optional['ExplorerManager'] = None

    def __init__(self):
        # Strict MRU records
        self._last_explorer_hwnd: Optional[int] = None
        self._last_explorer_path: Optional[str] = None
        self._last_explorer_time: float = 0.0

        # Focus Transition Tracking for Switch-Back Auto Switch
        self._prev_foreground_hwnd: Optional[int] = None
        self._last_transition_time: float = 0.0
        self._last_injected_dialog_hwnd: Optional[int] = None
        self._last_injected_path: Optional[str] = None

        # WinEventHook tracker state
        self._tracker_thread: Optional[threading.Thread] = None
        self._tracker_thread_id: Optional[int] = None
        self._tracker_running: bool = False
        self._winevent_hook = None
        self._winevent_proc_ref = None

        # Mode configurations
        self.auto_switch_enabled: bool = True
        self.on_dialog_auto_switch: Optional[Callable[[int], None]] = None

    @classmethod
    def get_instance(cls) -> 'ExplorerManager':
        if cls._instance is None:
            cls._instance = ExplorerManager()
        return cls._instance

    # ---------------------------------------------------------
    # WinEventHook Foreground Tracking
    # ---------------------------------------------------------
    def start_tracking(self, auto_switch: Optional[bool] = None, on_auto_switch: Optional[Callable[[int], None]] = None):
        """Starts background WinEventHook tracking for EVENT_SYSTEM_FOREGROUND."""
        if auto_switch is not None:
            self.auto_switch_enabled = auto_switch
        if on_auto_switch is not None:
            self.on_dialog_auto_switch = on_auto_switch

        if self._tracker_running:
            return

        self._tracker_running = True
        self._tracker_thread = threading.Thread(target=self._tracker_loop, name="ExplorerTrackerThread", daemon=True)
        self._tracker_thread.start()
        logger.info("[ExplorerManager] 资源管理器前台焦点追踪服务 (WinEventHook) 已启动")

        # Initial check for current foreground window
        self._check_initial_foreground()

    def stop_tracking(self):
        """Stops the background WinEventHook tracker."""
        if not self._tracker_running:
            return
        self._tracker_running = False
        if self._tracker_thread_id:
            user32.PostThreadMessageW(self._tracker_thread_id, win32con.WM_QUIT, 0, 0)
        logger.info("[ExplorerManager] 资源管理器前台焦点追踪服务已停止")

    def is_tracking(self) -> bool:
        return self._tracker_running

    def _check_initial_foreground(self):
        try:
            ensure_desktop()
            fg = self.get_foreground_hwnd()
            if fg and win32gui.IsWindow(fg):
                root = win32gui.GetAncestor(fg, win32con.GA_ROOT) or fg
                self._prev_foreground_hwnd = root
                if self.is_explorer_window(root):
                    path = self.get_explorer_path(root)
                    if path and os.path.exists(path):
                        self._last_explorer_hwnd = root
                        self._last_explorer_path = path
                        self._last_explorer_time = time.time()
                        logger.debug(f"[ExplorerManager] 初始捕获前台资源管理器: [HWND:{root}] 路径: {path}")
        except Exception as e:
            logger.debug(f"[ExplorerManager] 初始前台检查异常: {e}")

    def _tracker_loop(self):
        ensure_desktop()
        self._tracker_thread_id = kernel32.GetCurrentThreadId()

        def _callback(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
            if event == EVENT_SYSTEM_FOREGROUND and hwnd:
                try:
                    self._on_foreground_event(hwnd)
                except Exception as e:
                    logger.debug(f"[ExplorerManager] 前台事件处理异常: {e}")

        self._winevent_proc_ref = WINEVENTPROC(_callback)
        self._winevent_hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            None,
            ctypes.cast(self._winevent_proc_ref, ctypes.c_void_p),
            0,
            0,
            WINEVENT_OUTOFCONTEXT
        )

        if not self._winevent_hook:
            logger.warning(f"[ExplorerManager] SetWinEventHook 安装失败, code: {kernel32.GetLastError()}")
            self._tracker_running = False
            return

        msg = wintypes.MSG()
        while self._tracker_running:
            b_ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if b_ret == 0 or b_ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._winevent_hook:
            user32.UnhookWinEvent(self._winevent_hook)
            self._winevent_hook = None

    def _on_foreground_event(self, hwnd: int):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return

        try:
            root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd
        except Exception:
            root = hwnd
        prev_hwnd = self._prev_foreground_hwnd

        # Ignore repeated events on identical root window
        if prev_hwnd == root:
            return

        now = time.time()

        # 1. User switched into File Explorer
        if self.is_explorer_window(root):
            path = self.get_explorer_path(root)
            if path and os.path.exists(path):
                self._last_explorer_hwnd = root
                self._last_explorer_path = path
                self._last_explorer_time = now
                logger.debug(f"[ExplorerManager] MRU 记录更新: [HWND:{root}] 路径: {path}")
            self._prev_foreground_hwnd = root
            return

        # 2. User switched into File Dialog (#32770)
        if self.is_dialog_window(root):
            # Focus Transition Guard (状态跃迁精准判定):
            # Must satisfy: previous foreground window was a valid Explorer window
            is_switch_back = bool(prev_hwnd and win32gui.IsWindow(prev_hwnd) and self.is_explorer_window(prev_hwnd))

            # Debounce protection (< 300ms high-frequency switching between same pair of windows)
            is_debounced = (now - self._last_transition_time) >= 0.3

            if self.auto_switch_enabled and is_switch_back and is_debounced:
                self._last_transition_time = now
                logger.info(f"[ExplorerManager] 检测到正向切回 (Explorer [HWND:{prev_hwnd}] -> Dialog [HWND:{root}])，触发切回自动跳转")
                if self.on_dialog_auto_switch:
                    threading.Thread(target=self.on_dialog_auto_switch, args=(root,), daemon=True).start()
            else:
                if not is_switch_back:
                    logger.debug(f"[ExplorerManager] 切入对话框 [HWND:{root}]，但上一前台窗口 [HWND:{prev_hwnd}] 非资源管理器，安全静默")
                elif not is_debounced:
                    logger.debug(f"[ExplorerManager] 快速切换防抖拦截 (距上次切换仅 {now - self._last_transition_time:.3f}s)")

            self._prev_foreground_hwnd = root
            return

        # 3. Switched into any other third-party window (Chrome, WeChat, IDE, etc.)
        self._prev_foreground_hwnd = root

    # ---------------------------------------------------------
    # Window Classification & Query Helpers
    # ---------------------------------------------------------
    def is_explorer_window(self, hwnd: int) -> bool:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        try:
            cls_name = win32gui.GetClassName(hwnd)
            return cls_name in EXPLORER_CLASS_NAMES
        except Exception:
            return False

    def is_dialog_window(self, hwnd: int) -> bool:
        try:
            if not hwnd or not win32gui.IsWindow(hwnd):
                return False
            if not win32gui.IsWindowVisible(hwnd):
                return False
            cls_name = win32gui.GetClassName(hwnd)
            if cls_name not in DIALOG_CLASS_NAMES:
                return False
            rect = self.get_window_rect(hwnd)
            if not rect or (rect[2] - rect[0] <= 30) or (rect[3] - rect[1] <= 30):
                return False
            return True
        except Exception:
            return False

    def find_active_dialog(self, hwnd: Optional[int] = None) -> Optional[int]:
        """
        Locates the active file dialog (#32770) from the foreground window,
        popup windows, ancestors, or visible top-level windows.
        """
        ensure_desktop()
        if hwnd is None:
            hwnd = self.get_foreground_hwnd()

        if hwnd:
            if self.is_dialog_window(hwnd):
                return hwnd

            try:
                popup = win32gui.GetLastActivePopup(hwnd)
                if popup and popup != hwnd and self.is_dialog_window(popup):
                    return popup
            except Exception:
                pass

            try:
                root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
                if root and self.is_dialog_window(root):
                    return root
            except Exception:
                pass

        # Search visible top-level #32770 dialogs
        candidate = None
        def enum_cb(h, _):
            nonlocal candidate
            try:
                if self.is_dialog_window(h):
                    if candidate is None:
                        candidate = h
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            pass

        return candidate

    def get_foreground_hwnd(self) -> int:
        ensure_desktop()
        return win32gui.GetForegroundWindow()

    def get_window_rect(self, hwnd: int) -> Optional[Tuple[int, int, int, int]]:
        try:
            if hwnd and win32gui.IsWindow(hwnd):
                return win32gui.GetWindowRect(hwnd)
        except Exception:
            pass
        return None

    # ---------------------------------------------------------
    # Windows 11 Tabs & Path Extraction
    # ---------------------------------------------------------
    def get_active_tab_title(self, hwnd: int) -> str:
        """
        Retrieves the active tab title from top-level window title
        (stripping Explorer suffixes) or visible ShellTabWindowClass child.
        """
        active_title = ""

        # Priority 1: Top-level Explorer window title is kept in sync with the active tab
        try:
            raw_title = win32gui.GetWindowText(hwnd)
            for suffix in (' - 文件资源管理器', ' - File Explorer'):
                if raw_title.endswith(suffix):
                    raw_title = raw_title[:-len(suffix)]
            active_title = raw_title.strip()
        except Exception:
            pass

        if active_title:
            return active_title

        # Priority 2: ShellTabWindowClass child enumeration
        def enum_tabs(h, _):
            nonlocal active_title
            try:
                if win32gui.GetClassName(h) == 'ShellTabWindowClass':
                    if win32gui.IsWindowVisible(h):
                        txt = win32gui.GetWindowText(h)
                        if txt:
                            active_title = txt
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(hwnd, enum_tabs, None)
        except Exception:
            pass

        return active_title

    def get_explorer_path(self, hwnd: Optional[int] = None) -> Optional[str]:
        """
        Retrieves the active path from the specified Explorer window HWND
        (or foreground window if hwnd is None) using Shell.Application COM interface.
        Handles Windows 11 multi-tab discrimination.
        """
        ensure_desktop()
        if hwnd is None:
            hwnd = self.get_foreground_hwnd()

        if not hwnd or not self.is_explorer_window(hwnd):
            return None

        try:
            root_hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT) or hwnd
        except Exception:
            root_hwnd = hwnd
        active_tab_title = self.get_active_tab_title(root_hwnd)

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch('Shell.Application')
            windows = shell.Windows()
            count = windows.Count

            # Collect candidate windows belonging to this HWND
            candidates = []
            for i in range(count):
                try:
                    w = windows.Item(i)
                    if not w:
                        continue
                    w_hwnd = getattr(w, 'HWND', 0)
                    if not w_hwnd:
                        continue
                    w_root = win32gui.GetAncestor(w_hwnd, win32con.GA_ROOT) or w_hwnd

                    if w_hwnd == root_hwnd or w_root == root_hwnd or w_hwnd == hwnd:
                        loc_name = getattr(w, 'LocationName', '') or ''
                        candidates.append((w, loc_name))
                except Exception:
                    continue

            if not candidates:
                return None

            # If multiple candidates exist (Win 11 Tabs), find the one matching active_tab_title
            chosen_w = None
            if len(candidates) == 1:
                chosen_w = candidates[0][0]
            else:
                for w, loc_name in candidates:
                    if loc_name and active_tab_title and (loc_name.lower() == active_tab_title.lower() or active_tab_title.lower() in loc_name.lower()):
                        chosen_w = w
                        break
                if not chosen_w:
                    chosen_w = candidates[0][0]

            # Extract path from chosen_w
            # Attempt 1: Document.Folder.Self.Path
            try:
                folder_path = chosen_w.Document.Folder.Self.Path
                if folder_path and os.path.exists(folder_path):
                    return os.path.abspath(folder_path)
            except Exception:
                pass

            # Attempt 2: LocationURL (file:///...)
            try:
                loc_url = chosen_w.LocationURL
                if loc_url and loc_url.startswith('file:///'):
                    raw_path = loc_url[8:].replace('/', '\\')
                    unquoted = urllib.parse.unquote(raw_path)
                    if os.path.exists(unquoted):
                        return os.path.abspath(unquoted)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"[ExplorerManager] COM 提取路径异常: {e}")
        finally:
            pythoncom.CoUninitialize()

        return None

    # ---------------------------------------------------------
    # Strict MRU Path Retrieval (严格 MRU 定向原则)
    # ---------------------------------------------------------
    def get_last_active_explorer_path(self) -> Optional[str]:
        """
        Returns the active path of the LAST BROWSED / ACTIVATED Explorer window.

        STRICT MRU POLICY (严格 MRU 定向原则与零模糊兜底):
        1. If the foreground window is currently an Explorer window, update record and return its path.
        2. If no Explorer window was browsed (_last_explorer_hwnd is None), return None and remain silent.
           DO NOT fall back to arbitrary background Explorer windows!
        3. If the recorded window is no longer valid (IsWindow is False, user closed it),
           clear records, return None and remain silent.
           DO NOT fall back to arbitrary background Explorer windows!
        4. Re-verify the current active directory of _last_explorer_hwnd.
           If verified, return it.
        5. If COM momentarily fails but cached path exists on disk, return it.
        6. Otherwise return None.
        """
        ensure_desktop()

        # Step 1: Check current foreground first
        fg = self.get_foreground_hwnd()
        if fg and self.is_explorer_window(fg):
            root = win32gui.GetAncestor(fg, win32con.GA_ROOT) or fg
            curr_path = self.get_explorer_path(root)
            if curr_path and os.path.exists(curr_path):
                self._last_explorer_hwnd = root
                self._last_explorer_path = curr_path
                self._last_explorer_time = time.time()
                return curr_path

        # Step 2: Strict MRU check: must have prior browse record
        if not self._last_explorer_hwnd:
            logger.info("[ExplorerManager] [严格MRU定向] 无前序资源管理器浏览记录，拒绝注入并安全静默")
            return None

        # Step 3: Lifetime check: window must still exist
        if not win32gui.IsWindow(self._last_explorer_hwnd):
            logger.info(f"[ExplorerManager] [严格MRU定向] 上一个浏览的窗口 [HWND:{self._last_explorer_hwnd}] 已关闭，记录失效，拒绝注入并安全静默")
            self._last_explorer_hwnd = None
            self._last_explorer_path = None
            return None

        # Step 4: Re-query path of this specific window
        verified_path = self.get_explorer_path(self._last_explorer_hwnd)
        if verified_path and os.path.exists(verified_path):
            self._last_explorer_path = verified_path
            return verified_path

        # Step 5: Fall back to previously verified path of this specific window if directory still exists
        if self._last_explorer_path and os.path.exists(self._last_explorer_path):
            return self._last_explorer_path

        # Step 6: Strict rejection: do NOT iterate over arbitrary windows
        logger.info(f"[ExplorerManager] [严格MRU定向] 窗口 [HWND:{self._last_explorer_hwnd}] 路径无法确认，拒绝注入")
        return None

    def reset_mru(self):
        """Resets the MRU record (for testing and cleanup)."""
        self._last_explorer_hwnd = None
        self._last_explorer_path = None
        self._last_explorer_time = 0.0
