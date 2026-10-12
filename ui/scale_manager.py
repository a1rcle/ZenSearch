"""
ZenSearch - Dynamic UI Scale Manager & Multi-DPI Adaptor
Implements Section IV of ZenSearch技术方案.md:
- Windows Per-Monitor DPI Aware V2 initialization
- Dynamic resolution factor S_res and DPI scale S_dpi calculation
- Responsive width clamping and font/padding scaling (dp, pt)
- Focus screen sniffing (mouse cursor -> foreground window -> primary screen)
- Safe screen available geometry positioning & boundary clamping
"""

import sys
import math
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple
import win32gui
from PyQt5.QtCore import Qt, QPoint, QRect
from PyQt5.QtGui import QCursor, QScreen
from PyQt5.QtWidgets import QApplication

from core.logger import logger


def init_high_dpi_context():
    """
    Registers Per-Monitor DPI Aware V2 before QApplication creation,
    and sets Qt AA_EnableHighDpiScaling and AA_UseHighDpiPixmaps.
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        logger.debug("[UiScaleManager] 成功注册 Windows Per-Monitor DPI Aware V2")
    except Exception as e:
        try:
            # Fallback to Win8.1 / early Win10: PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            logger.debug("[UiScaleManager] 回退注册 PROCESS_PER_MONITOR_DPI_AWARE = 2")
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
                logger.debug("[UiScaleManager] 回退注册 SetProcessDPIAware")
            except Exception:
                pass

    # Enable Qt native High-DPI scaling
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)


class UiScaleManager:
    """
    Central scale and responsive geometry manager for ZenSearch UI.
    Computes scale metrics per target screen based on resolution and DPI.
    """
    _instance: Optional['UiScaleManager'] = None

    @classmethod
    def get_instance(cls) -> 'UiScaleManager':
        if cls._instance is None:
            cls._instance = UiScaleManager()
        return cls._instance

    def __init__(self):
        self._cached_screen: Optional[QScreen] = None
        self._s_res: float = 1.0
        self._s_dpi: float = 1.0

    def get_target_screen(self, fallback_hwnd: Optional[int] = None) -> QScreen:
        """
        Screen sniffing priority according to spec:
        1. Screen containing current mouse cursor: QApplication.screenAt(QCursor.pos())
        2. Screen containing current foreground window (or fallback_hwnd)
        3. Primary screen fallback
        """
        app = QApplication.instance()
        if not app:
            return None

        # Step 1: Mouse cursor position
        cursor_pos = QCursor.pos()
        screen = app.screenAt(cursor_pos)
        if screen:
            return screen

        # Step 2: Foreground window position
        target_hwnd = fallback_hwnd or win32gui.GetForegroundWindow()
        if target_hwnd and win32gui.IsWindow(target_hwnd):
            try:
                rect = win32gui.GetWindowRect(target_hwnd)
                # Center point of target window
                center_x = (rect[0] + rect[2]) // 2
                center_y = (rect[1] + rect[3]) // 2
                screen = app.screenAt(QPoint(center_x, center_y))
                if screen:
                    return screen
            except Exception:
                pass

        # Step 3: Primary screen fallback
        primary = app.primaryScreen()
        return primary or (app.screens()[0] if app.screens() else None)

    def update_for_screen(self, screen: Optional[QScreen] = None):
        """
        Updates scale factors for the given screen (or detected target screen).
        Base canvas: 1920 x 1080 (100% scale, DPR 1.0)
        """
        if screen is None:
            screen = self.get_target_screen()

        if not screen:
            self._s_res = 1.0
            self._s_dpi = 1.0
            return

        self._cached_screen = screen
        self._s_dpi = screen.devicePixelRatio()

        geom = screen.geometry()
        w = max(geom.width(), 800)
        h = max(geom.height(), 600)

        # In Qt with AA_EnableHighDpiScaling, geom is DIP (logical pixels).
        # We also compute physical pixels to gauge actual canvas area:
        phys_w = w * self._s_dpi
        phys_h = h * self._s_dpi

        # Resolution adaptive factor S_res based on physical canvas relative to 1920x1080
        # S_res = clamp(0.85, sqrt((phys_w * phys_h) / (1920 * 1080)), 1.30)
        area_ratio = (phys_w * phys_h) / (1920.0 * 1080.0)
        raw_s_res = math.sqrt(area_ratio)

        # Qt's DIP scaling already scales by S_dpi. So relative styling DIP adjustments
        # should only scale by the relative resolution divergence:
        # S_res_dip = raw_s_res / S_dpi, clamped between 0.85 and 1.30
        res_factor = raw_s_res / max(self._s_dpi, 1.0)
        self._s_res = max(0.85, min(res_factor, 1.30))

    @property
    def scale_res(self) -> float:
        return self._s_res

    @property
    def scale_dpi(self) -> float:
        return self._s_dpi

    def dp(self, value: float) -> int:
        """
        Linearly scales a metric in DIP based on screen resolution factor.
        """
        return max(1, int(round(value * self._s_res)))

    def pt(self, font_size: float) -> int:
        """
        Scales font size points.
        """
        return max(8, int(round(font_size * self._s_res)))

    def compute_launcher_width(self, screen: Optional[QScreen] = None) -> int:
        """
        W_launcher = clamp(560, int(W_avail * 0.38), 920)
        Scaled further if S_res is significantly different from 1.0.
        """
        if not screen:
            screen = self.get_target_screen()
        if not screen:
            return 720

        avail = screen.availableGeometry()
        w_avail = avail.width()
        calculated = int(w_avail * 0.38)
        # Clamped within [560, 920] DIP
        return max(560, min(calculated, 920))

    def compute_launcher_max_visible_items(self, screen: Optional[QScreen] = None) -> int:
        """
        Max visible item count based on available height:
        H_avail <= 800: 6
        800 < H_avail <= 1200: 8
        H_avail > 1200: 10
        """
        if not screen:
            screen = self.get_target_screen()
        if not screen:
            return 8

        avail = screen.availableGeometry()
        h_avail = avail.height()
        if h_avail <= 800:
            return 6
        elif h_avail <= 1200:
            return 8
        else:
            return 10

    def compute_launcher_position(self, window_width: int, window_height: int, screen: Optional[QScreen] = None) -> Tuple[int, int]:
        """
        X = geom.x() + (geom.width() - W_launcher) // 2
        Y = geom.y() + int(geom.height() * 0.22)
        Safe within availableGeometry to avoid taskbar clipping.
        """
        if not screen:
            screen = self.get_target_screen()
        if not screen:
            return 100, 100

        geom = screen.availableGeometry()
        x = geom.x() + (geom.width() - window_width) // 2
        y = geom.y() + int(geom.height() * 0.22)

        # Boundary clamping within available geometry
        x = max(geom.x(), min(x, geom.right() - window_width))
        y = max(geom.y(), min(y, geom.bottom() - window_height))
        return x, y

    def compute_companion_width(self, explorer_w: int) -> int:
        """
        W_companion = clamp(400, int(W_exp * 0.52), 640)
        """
        calculated = int(explorer_w * 0.52)
        return max(400, min(calculated, 640))

    def compute_companion_position(
        self,
        exp_rect: Tuple[int, int, int, int],
        comp_w: int,
        comp_h: int,
        screen: Optional[QScreen] = None
    ) -> Tuple[int, int]:
        """
        Positions companion bar at Explorer's bottom right with boundary clamping
        against the display's availableGeometry (safe against taskbar and screen edges).
        exp_rect = (l, t, r, b)
        """
        l, t, r, b = exp_rect
        if not screen:
            # Detect screen by explorer center
            app = QApplication.instance()
            center_pt = QPoint((l + r) // 2, (t + b) // 2)
            screen = app.screenAt(center_pt) if app else None
            if not screen:
                screen = self.get_target_screen()

        if not screen:
            return r - comp_w - 25, b - comp_h - 20

        avail = screen.availableGeometry()
        s_left = avail.left()
        s_top = avail.top()
        s_right = avail.right()
        s_bottom = avail.bottom()

        # Target dock: near bottom-right of Explorer
        x_target = r - comp_w - self.dp(25)
        y_target = b - comp_h - self.dp(20)

        # Clamping to stay safely on screen
        safe_margin = self.dp(10)
        clamped_x = max(s_left + safe_margin, min(x_target, s_right - comp_w - safe_margin))
        clamped_y = max(s_top + safe_margin, min(y_target, s_bottom - comp_h - safe_margin))

        return clamped_x, clamped_y


def get_launcher_qss(scale_mgr: UiScaleManager) -> str:
    """
    Parametric QSS generator for LauncherWindow using UiScaleManager.
    """
    return f"""
        QWidget#Container {{
            background-color: #1E1F29;
            border: 1px solid #323545;
            border-radius: {scale_mgr.dp(12)}px;
        }}
        QLineEdit {{
            background: transparent;
            border: none;
            color: #FFFFFF;
            font-size: {scale_mgr.pt(16)}px;
            selection-background-color: #4C566A;
        }}
        QListWidget {{
            background: transparent;
            border: none;
            outline: none;
        }}
        QListWidget::item {{
            border-radius: {scale_mgr.dp(8)}px;
            padding: {scale_mgr.dp(2)}px {scale_mgr.dp(4)}px;
        }}
        QListWidget::item:selected {{
            background-color: #2E3440;
        }}
        QListWidget::item:hover:!selected {{
            background-color: #252834;
        }}
    """


def get_companion_qss(scale_mgr: UiScaleManager) -> str:
    """
    Parametric QSS generator for CompanionBar using UiScaleManager.
    """
    return f"""
        QWidget#CompanionContainer {{
            background-color: #1A1B23;
            border: 1px solid #3E4451;
            border-radius: {scale_mgr.dp(10)}px;
        }}
        QLineEdit {{
            background: transparent;
            border: none;
            color: #FFFFFF;
            font-size: {scale_mgr.pt(14)}px;
            selection-background-color: #3E4451;
        }}
        QListWidget {{
            background: transparent;
            border: none;
            outline: none;
        }}
        QListWidget::item {{
            border-radius: {scale_mgr.dp(6)}px;
            padding: {scale_mgr.dp(1)}px {scale_mgr.dp(2)}px;
        }}
        QListWidget::item:selected {{
            background-color: #2C313C;
        }}
        QListWidget::item:hover:!selected {{
            background-color: #21252B;
        }}
    """
