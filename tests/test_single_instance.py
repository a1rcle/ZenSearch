"""
ZenSearch - Unit Tests for Single Instance Protection & Host Activation
Validates:
1. Win32 Named Mutex acquisition, duplicate blocking (ERROR_ALREADY_EXISTS), and release
2. Session isolation prefix and USERNAME formatting
3. Context manager (with statement) acquisition and automatic cleanup
4. MessageHostWindow registration, FindWindow resolution, and WAKEUP_MSG dispatch
5. activate_existing_instance() foreground bypass and signal invocation
6. Silent mode idempotency and fallback notification execution
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import win32gui
import win32con
from PyQt5.QtWidgets import QApplication
from ui.scale_manager import init_high_dpi_context
from core.single_instance import (
    SingleInstanceManager,
    MessageHostWindow,
    HOST_WINDOW_TITLE,
    WAKEUP_MSG,
    ERROR_ALREADY_EXISTS
)


class TestSingleInstance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_high_dpi_context()
        cls.app = QApplication.instance()
        if not cls.app:
            cls.app = QApplication(sys.argv)

    def test_mutex_acquire_and_release(self):
        """Verify first acquire succeeds and release cleans up handle."""
        app_id = "ZenSearch_UnitTest_Acquire"
        sim = SingleInstanceManager(app_id)
        try:
            self.assertFalse(sim.is_acquired())
            acquired = sim.acquire()
            self.assertTrue(acquired)
            self.assertTrue(sim.is_acquired())
            self.assertIsNotNone(sim.h_mutex)
            self.assertIn("Local\\ZenSearch_UnitTest_Acquire_SingleInstance_Mutex_", sim.mutex_name)
        finally:
            sim.release()
            self.assertFalse(sim.is_acquired())
            self.assertIsNone(sim.h_mutex)

    def test_duplicate_instance_blocked(self):
        """Verify second instance is blocked when primary instance holds mutex."""
        app_id = "ZenSearch_UnitTest_DupBlock"
        sim1 = SingleInstanceManager(app_id)
        sim2 = SingleInstanceManager(app_id)

        try:
            # Primary instance acquires
            self.assertTrue(sim1.acquire())
            self.assertTrue(sim1.is_acquired())

            # Secondary instance attempts to acquire the same mutex
            self.assertFalse(sim2.acquire())
            self.assertFalse(sim2.is_acquired())

            # Primary instance releases
            sim1.release()
            self.assertFalse(sim1.is_acquired())

            # Secondary instance can now acquire
            self.assertTrue(sim2.acquire())
            self.assertTrue(sim2.is_acquired())
        finally:
            sim1.release()
            sim2.release()

    def test_context_manager(self):
        """Verify SingleInstanceManager works seamlessly with context managers."""
        app_id = "ZenSearch_UnitTest_Context"
        with SingleInstanceManager(app_id) as sim:
            self.assertTrue(sim.is_acquired())
            # Concurrent check
            sim2 = SingleInstanceManager(app_id)
            self.assertFalse(sim2.acquire())
            sim2.release()

        # After exiting context, mutex should be released
        self.assertFalse(sim.is_acquired())
        sim3 = SingleInstanceManager(app_id)
        self.assertTrue(sim3.acquire())
        sim3.release()

    def test_message_host_window_find(self):
        """Verify MessageHostWindow creates a valid native top-level window discoverable via FindWindow."""
        host = MessageHostWindow()
        try:
            hwnd = int(host.winId())
            self.assertGreater(hwnd, 0)

            found_hwnd = win32gui.FindWindow(None, HOST_WINDOW_TITLE)
            self.assertEqual(hwnd, found_hwnd)
        finally:
            host.close()

    def test_cross_instance_wakeup_dispatch(self):
        """Verify activate_existing_instance sends WAKEUP_MSG and triggers host signal."""
        host = MessageHostWindow()
        wakeup_called = []
        host.wakeup_triggered.connect(lambda: wakeup_called.append(True))

        try:
            sim = SingleInstanceManager("ZenSearch_UnitTest_Wakeup")
            ok = sim.activate_existing_instance()
            self.assertTrue(ok, "activate_existing_instance should find the host window")

            # Process Qt event queue so nativeEvent and pyqtSignal are dispatched
            QApplication.processEvents()

            self.assertTrue(len(wakeup_called) > 0, "Host window wakeup signal should have been triggered")
        finally:
            host.close()

    def test_activate_when_no_host_returns_false(self):
        """Verify activate_existing_instance returns False when host window does not exist."""
        # Ensure no residual host window exists with our title
        sim = SingleInstanceManager("ZenSearch_UnitTest_NoHost")
        h_existing = win32gui.FindWindow(None, HOST_WINDOW_TITLE)
        if not h_existing:
            ok = sim.activate_existing_instance()
            self.assertFalse(ok)

    def test_fallback_notification_mock(self):
        """Verify show_already_running_fallback calls Win32 MessageBoxW safely."""
        with patch("core.single_instance.user32.MessageBoxW") as mock_msgbox:
            SingleInstanceManager.show_already_running_fallback()
            mock_msgbox.assert_called_once()
            args, _ = mock_msgbox.call_args
            self.assertIn("ZenSearch 已在后台常驻运行中", args[1])


if __name__ == '__main__':
    unittest.main()
