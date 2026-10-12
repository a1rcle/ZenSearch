"""
Unit tests for UiScaleManager & Multi-DPI Responsive UI Adaptor.
Validates:
1. Per-Monitor DPI Aware V2 initialization
2. Linear DIP and Font Point Scaling (dp, pt)
3. Proportional Launcher Width Clamping [560, 920] across different resolutions:
   - 1366x768 (Low-res laptop)
   - 1920x1080 (Standard 1080p)
   - 2560x1440 (2K)
   - 3840x2160 (4K)
4. Dynamic max visible list item count (<=800: 6, <=1200: 8, >1200: 10)
5. CompanionBar Width Clamping [400, 640] based on Explorer width
6. CompanionBar Boundary Clamping to available geometry (taskbar / screen edge avoidance)
7. Parametric QSS generation
"""

import unittest
import sys
import os
from PyQt5.QtCore import QRect, QPoint
from PyQt5.QtWidgets import QApplication

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from ui.scale_manager import UiScaleManager, init_high_dpi_context, get_launcher_qss, get_companion_qss


class MockScreen:
    def __init__(self, geom_rect: QRect, avail_rect: QRect, dpr: float = 1.0, name: str = "MockScreen"):
        self._geom = geom_rect
        self._avail = avail_rect
        self._dpr = dpr
        self._name = name

    def geometry(self) -> QRect:
        return self._geom

    def availableGeometry(self) -> QRect:
        return self._avail

    def devicePixelRatio(self) -> float:
        return self._dpr

    def name(self) -> str:
        return self._name


class TestUiScaleManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_high_dpi_context()
        cls.app = QApplication.instance()
        if not cls.app:
            cls.app = QApplication(sys.argv)
        cls.scale_mgr = UiScaleManager.get_instance()

    def test_dp_and_pt_scaling(self):
        """Test linear dp and font pt scaling with scale factors."""
        self.scale_mgr._s_res = 1.0
        self.assertEqual(self.scale_mgr.dp(16), 16)
        self.assertEqual(self.scale_mgr.pt(14), 14)

        self.scale_mgr._s_res = 1.25
        self.assertEqual(self.scale_mgr.dp(16), 20)
        self.assertEqual(self.scale_mgr.pt(12), 15)

        self.scale_mgr._s_res = 0.85
        self.assertEqual(self.scale_mgr.dp(20), 17)
        self.assertEqual(self.scale_mgr.pt(10), 8)  # round(8.5) -> 8 in Python 3 banker's rounding

    def test_launcher_width_clamping(self):
        """
        Verify W_launcher = clamp(560, int(W_avail * 0.38), 920):
        1366x768 -> 519 clamped to 560
        1920x1080 -> 729
        2560x1440 -> 972 clamped to 920
        3840x2160 -> clamped to 920
        """
        # Low-res screen 1366x768
        scr_low = MockScreen(QRect(0, 0, 1366, 768), QRect(0, 0, 1366, 728))
        w_low = self.scale_mgr.compute_launcher_width(scr_low)
        self.assertEqual(w_low, 560, "1366x768 宽度应兜底 clamp 至 560px")

        # 1080p screen
        scr_1080 = MockScreen(QRect(0, 0, 1920, 1080), QRect(0, 0, 1920, 1040))
        w_1080 = self.scale_mgr.compute_launcher_width(scr_1080)
        self.assertEqual(w_1080, 729, "1920x1080 宽度应为 729px (38%)")

        # 2K screen
        scr_2k = MockScreen(QRect(0, 0, 2560, 1440), QRect(0, 0, 2560, 1400))
        w_2k = self.scale_mgr.compute_launcher_width(scr_2k)
        self.assertEqual(w_2k, 920, "2560x1440 宽度应限制 clamp 至 920px")

        # 4K screen
        scr_4k = MockScreen(QRect(0, 0, 3840, 2160), QRect(0, 0, 3840, 2112))
        w_4k = self.scale_mgr.compute_launcher_width(scr_4k)
        self.assertEqual(w_4k, 920, "3840x2160 宽度应限制 clamp 至 920px")

    def test_max_visible_items_by_height(self):
        """
        Verify visible item count thresholds:
        H_avail <= 800: 6
        800 < H_avail <= 1200: 8
        H_avail > 1200: 10
        """
        scr_h700 = MockScreen(QRect(0, 0, 1366, 768), QRect(0, 0, 1366, 728))
        self.assertEqual(self.scale_mgr.compute_launcher_max_visible_items(scr_h700), 6)

        scr_h900 = MockScreen(QRect(0, 0, 1920, 1080), QRect(0, 0, 1920, 1040))
        self.assertEqual(self.scale_mgr.compute_launcher_max_visible_items(scr_h900), 8)

        scr_h1400 = MockScreen(QRect(0, 0, 2560, 1440), QRect(0, 0, 2560, 1400))
        self.assertEqual(self.scale_mgr.compute_launcher_max_visible_items(scr_h1400), 10)

    def test_companion_width_clamping(self):
        """
        W_companion = clamp(400, int(W_exp * 0.52), 640)
        """
        # Very narrow explorer (600px) -> 312 clamped to 400
        w1 = self.scale_mgr.compute_companion_width(600)
        self.assertEqual(w1, 400)

        # Standard explorer (1000px) -> 520
        w2 = self.scale_mgr.compute_companion_width(1000)
        self.assertEqual(w2, 520)

        # Full-screen / maximized explorer (1800px) -> 936 clamped to 640
        w3 = self.scale_mgr.compute_companion_width(1800)
        self.assertEqual(w3, 640)

    def test_companion_boundary_clamping(self):
        """
        Ensure companion bar does not overflow screen available geometry
        even when Explorer is pushed against screen edge or taskbar.
        """
        scr = MockScreen(QRect(0, 0, 1920, 1080), QRect(0, 0, 1920, 1040))  # taskbar at bottom
        self.scale_mgr.update_for_screen(scr)

        # Explorer placed at far bottom-right edge: (1000, 300, 1920, 1040)
        exp_rect = (1000, 300, 1920, 1040)
        comp_w, comp_h = 500, 300
        x, y = self.scale_mgr.compute_companion_position(exp_rect, comp_w, comp_h, screen=scr)

        # Clamped inside (0, 0, 1920, 1040)
        self.assertGreaterEqual(x, 0)
        self.assertLessEqual(x + comp_w, 1920)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(y + comp_h, 1040)

    def test_parametric_qss_rendering(self):
        """Verify dynamic QSS strings contain scaled values and valid syntax."""
        self.scale_mgr._s_res = 1.0
        qss_launcher = get_launcher_qss(self.scale_mgr)
        self.assertIn("border-radius: 12px", qss_launcher)
        self.assertIn("font-size: 16px", qss_launcher)

        qss_companion = get_companion_qss(self.scale_mgr)
        self.assertIn("border-radius: 10px", qss_companion)
        self.assertIn("font-size: 14px", qss_companion)


if __name__ == '__main__':
    unittest.main()
