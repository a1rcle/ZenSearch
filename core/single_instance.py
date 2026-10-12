"""
ZenSearch - Single Instance Manager & Cross-Instance Activation Host
Based on ZenSearch技术方案.md (三.6 & 七.4.7):
- Win32 Named Mutex (CreateMutexW) for zero-overhead, crash-safe single instance locking.
- Session isolation using "Local\\ZenSearch_SingleInstance_Mutex_{USERNAME}".
- Cross-instance activation host window (ZenSearch_MessageHost) & WAKEUP_MSG (WM_USER + 1024).
- Foreground lock bypass via AllowSetForegroundWindow(ASFW_ANY / pid).
- Silent launch idempotency support (--silent).
- Fallback notification when main host is not immediately addressable.
"""

import os
import sys
import ctypes
from ctypes import wintypes
from typing import Optional

import win32gui
import win32con
import win32process

from core.logger import logger

# Win32 API functions & constants
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

ERROR_ALREADY_EXISTS = 183
WAKEUP_MSG = win32con.WM_USER + 1024
HOST_WINDOW_TITLE = "ZenSearch_MessageHost"


class SingleInstanceManager:
    """
    Manages single-instance lifecycle using Windows Named Mutex and handles
    cross-instance communication to activate existing running instances.
    """

    def __init__(self, app_id: str = "ZenSearch"):
        self.app_id = app_id
        user = os.environ.get("USERNAME", "default")
        # Local\\ prefix restricts mutex to the current user's interactive logon session
        self.mutex_name = f"Local\\{app_id}_SingleInstance_Mutex_{user}"
        self.h_mutex: Optional[wintypes.HANDLE] = None
        self._acquired: bool = False

    def acquire(self) -> bool:
        """
        Attempts to acquire the single instance named mutex.
        Returns:
            True if this is the first/primary instance (mutex acquired).
            False if an existing instance is already running (ERROR_ALREADY_EXISTS).
        """
        self.h_mutex = kernel32.CreateMutexW(None, False, self.mutex_name)
        last_err = kernel32.GetLastError()

        if last_err == ERROR_ALREADY_EXISTS:
            logger.info(f"[SingleInstance] 检测到已有实例正在运行 (Mutex: {self.mutex_name})")
            # Close the duplicate handle opened for the existing mutex
            if self.h_mutex:
                kernel32.CloseHandle(self.h_mutex)
                self.h_mutex = None
            self._acquired = False
            return False

        if not self.h_mutex:
            logger.warning(f"[SingleInstance] 创建互斥体失败，系统错误码: {last_err}")
            # Non-fatal fallback: allow starting to avoid blocking the user
            self._acquired = True
            return True

        self._acquired = True
        logger.debug(f"[SingleInstance] 成功独占单实例互斥锁: {self.mutex_name}")
        return True

    def activate_existing_instance(self) -> bool:
        """
        Signals the already-running instance to bring its main launcher to the foreground.
        Uses AllowSetForegroundWindow to grant foreground focus permissions across process boundaries.
        Returns:
            True if the running host window was found and the wakeup message was posted.
            False if the host window was not found.
        """
        h_host = win32gui.FindWindow(None, HOST_WINDOW_TITLE)
        if h_host and win32gui.IsWindow(h_host):
            # 1. Bypass Windows Foreground Lock
            # ASFW_ANY = -1 (0xFFFFFFFF) allows any process to set foreground window
            try:
                user32.AllowSetForegroundWindow(ctypes.c_uint(-1))
            except Exception:
                pass

            try:
                _, pid = win32process.GetWindowThreadProcessId(h_host)
                if pid:
                    user32.AllowSetForegroundWindow(ctypes.c_uint(pid))
            except Exception:
                pass

            # 2. Post wakeup message to host window
            win32gui.PostMessage(h_host, WAKEUP_MSG, 0, 0)
            logger.info(f"[SingleInstance] 已向后台已有实例 [HWND:{h_host}] 发送唤醒指令")
            return True

        logger.warning("[SingleInstance] 未找到后台实例的消息宿主窗口 (ZenSearch_MessageHost)")
        return False

    def is_acquired(self) -> bool:
        """Returns True if the mutex is currently held by this instance."""
        return self._acquired and self.h_mutex is not None

    def release(self):
        """Explicitly releases the named mutex handle."""
        if self.h_mutex:
            try:
                kernel32.CloseHandle(self.h_mutex)
            except Exception:
                pass
            self.h_mutex = None
            self._acquired = False
            logger.debug(f"[SingleInstance] 已释放单实例互斥锁: {self.mutex_name}")

    @staticmethod
    def show_already_running_fallback():
        """
        Shows a fallback informational alert when secondary launch detects an
        existing instance but the message host window cannot be reached.
        """
        try:
            MB_OK = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_SETFOREGROUND = 0x00010000
            user32.MessageBoxW(
                0,
                "ZenSearch 已在后台常驻运行中。\n\n• 双击 Ctrl 即可呼出全局搜索浮窗\n• 文件对话框中按 Ctrl + G 即可重定向路径",
                "ZenSearch 提示",
                MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND
            )
        except Exception:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __del__(self):
        self.release()


# --------------------------------------------------------------------------
# Message Host Window (Qt-based invisible top-level message sink)
# --------------------------------------------------------------------------
try:
    from PyQt5.QtCore import pyqtSignal
    from PyQt5.QtWidgets import QWidget

    class MessageHostWindow(QWidget):
        """
        Invisible native message sink window for cross-instance communication.
        Catches WAKEUP_MSG posted from secondary instances and emits wakeup_triggered.
        """
        wakeup_triggered = pyqtSignal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle(HOST_WINDOW_TITLE)
            # Access winId() to force Win32 HWND creation immediately
            self._hwnd = int(self.winId())
            # Explicitly sync Win32 window text for FindWindow matching
            win32gui.SetWindowText(self._hwnd, HOST_WINDOW_TITLE)

        def nativeEvent(self, eventType, message):
            """
            Filters native Windows messages for the hidden host window.
            """
            if eventType in (b'windows_generic_MSG', 'windows_generic_MSG'):
                try:
                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == WAKEUP_MSG:
                        logger.info("[SingleInstance] 消息宿主窗口接收到二次启动唤醒指令")
                        self.wakeup_triggered.emit()
                        return True, 0
                except Exception as ex:
                    logger.debug(f"[SingleInstance] 解析原生消息异常: {ex}")
            return super().nativeEvent(eventType, message)

except ImportError:
    # In headless / test environments where PyQt5 might not be present
    class MessageHostWindow:
        pass
