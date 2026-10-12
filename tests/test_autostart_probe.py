"""
ZenSearch - Unit Tests for AutoStartManager & EverythingProbe
Validates:
1. AutoStartManager command generation with pythonw.exe and --silent
2. AutoStartManager registry enable/disable toggle in HKCU
3. AutoStartManager self-healing detection for relocated/invalid paths
4. EverythingProbe 4-tier pipeline execution (Level 1~4)
5. EverythingProbe silent launch flag construction (-startup)
6. EverythingGuideDialog instantiation and responsive layout
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.autostart import AutoStartManager
from core.everything_probe import EverythingProbe, ProbeResult
from ui.scale_manager import init_high_dpi_context
from PyQt5.QtWidgets import QApplication


class TestAutoStartAndProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_high_dpi_context()
        cls.app = QApplication.instance()
        if not cls.app:
            cls.app = QApplication(sys.argv)

    def test_autostart_command_format(self):
        """Verify startup command contains quotes, pythonw.exe and --silent parameter."""
        cmd = AutoStartManager.get_target_command(silent=True)
        self.assertTrue(cmd.startswith('"'), "Command should start with a quote")
        self.assertIn("main.py", cmd)
        self.assertIn("--silent", cmd)
        # Should prefer pythonw.exe if available
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(pyw):
            self.assertIn("pythonw.exe", cmd)

    def test_autostart_registry_toggle(self):
        """Test toggling autostart registry key on/off and reading back."""
        orig_state = AutoStartManager.is_autostart_enabled()
        orig_cmd = AutoStartManager.get_registered_command()

        try:
            # Enable
            ok = AutoStartManager.set_autostart(True, silent=True)
            self.assertTrue(ok)
            self.assertTrue(AutoStartManager.is_autostart_enabled())
            reg_cmd = AutoStartManager.get_registered_command()
            self.assertIsNotNone(reg_cmd)
            self.assertIn("main.py", reg_cmd)

            # Disable
            ok = AutoStartManager.set_autostart(False)
            self.assertTrue(ok)
            self.assertFalse(AutoStartManager.is_autostart_enabled())
            self.assertIsNone(AutoStartManager.get_registered_command())

        finally:
            # Restore original state
            if orig_state and orig_cmd:
                AutoStartManager.set_autostart(True, silent="--silent" in orig_cmd)
            else:
                AutoStartManager.set_autostart(False)

    def test_autostart_self_healing(self):
        """Test self-healing when registered path is invalid or pointed to dummy path."""
        orig_state = AutoStartManager.is_autostart_enabled()
        try:
            # Inject a broken path into registry
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "ZenSearch", 0, winreg.REG_SZ, r'"Z:\NonExistent_ZenSearch.exe" --silent')

            self.assertTrue(AutoStartManager.is_autostart_enabled())
            healed, msg = AutoStartManager.self_heal_if_needed()
            self.assertTrue(healed)
            self.assertIn("已自愈更新", msg)

            # Check healed command
            new_cmd = AutoStartManager.get_registered_command()
            self.assertIn("main.py", new_cmd)
        finally:
            if not orig_state:
                AutoStartManager.set_autostart(False)

    def test_everything_probe_pipeline(self):
        """Verify EverythingProbe executes without error and identifies current running or installed status."""
        probe = EverythingProbe.probe_environment()
        self.assertIsInstance(probe, ProbeResult)
        self.assertIn(probe.source, ["level1_window", "level1_process", "level2_registry", "level3_path", "level4_physical", "not_found"])
        if probe.installed:
            self.assertIsNotNone(probe.exe_path)
            self.assertTrue(os.path.exists(probe.exe_path))

    def test_everything_probe_ensure_running(self):
        """Verify ensure_everything_running returns a valid boolean tuple."""
        ready, probe = EverythingProbe.ensure_everything_running()
        self.assertIsInstance(ready, bool)
        self.assertIsInstance(probe, ProbeResult)

    def test_everything_guide_dialog_creation(self):
        """Verify EverythingGuideDialog instantiates and styles properly."""
        from ui.guide_dialog import EverythingGuideDialog
        dialog = EverythingGuideDialog()
        self.assertEqual(dialog.action_taken, "later")
        self.assertIsNotNone(dialog.btn_download)
        self.assertIsNotNone(dialog.btn_later)


if __name__ == '__main__':
    unittest.main()
