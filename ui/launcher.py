"""
ZenSearch - Main Global Search Launcher
Spotlight / Listary style modern floating search bar.
Multi-DPI & Responsive UI adapted based on ZenSearch技术方案.md:
- Dynamic sizing via UiScaleManager & responsive width clamping
- Parametric QSS stylesheet with dp() and pt() scaling
- Screen sniffing priority (mouse cursor -> active window -> primary screen)
- Safe positioning using screen availableGeometry (taskbar collision avoidance)
- Cross-screen migration and DPI change handling (screenChanged, WM_DPICHANGED)
"""

import os
import subprocess
from typing import List, Optional
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QPoint, QEvent
from PyQt5.QtGui import QIcon, QFont, QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QLabel, QGraphicsDropShadowEffect, QApplication
)

import ctypes
from ctypes import wintypes
import win32gui

from core.everything_client import EverythingClient, SearchResult
from ui.scale_manager import UiScaleManager, get_launcher_qss


class SearchWorker(QThread):
    results_ready = pyqtSignal(list)

    def __init__(self, query: str, max_results: int = 50):
        super().__init__()
        self.query = query
        self.max_results = max_results
        self.client = EverythingClient.get_instance()

    def run(self):
        results = self.client.search(self.query, max_results=self.max_results)
        self.results_ready.emit(results)


class ResultItemWidget(QWidget):
    def __init__(self, item: SearchResult, scale_mgr: UiScaleManager, parent=None):
        super().__init__(parent)
        self.scale_mgr = scale_mgr

        layout = QVBoxLayout(self)
        pad_h = self.scale_mgr.dp(10)
        pad_v = self.scale_mgr.dp(6)
        layout.setContentsMargins(pad_h, pad_v, pad_h, pad_v)
        layout.setSpacing(self.scale_mgr.dp(2))

        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(self.scale_mgr.dp(8))

        # Icon tag
        icon_label = QLabel("📁" if item.is_folder else "📄")
        icon_label.setStyleSheet(f"font-size: {self.scale_mgr.pt(14)}px;")
        top_layout.addWidget(icon_label)

        # File Name
        self.name_label = QLabel(item.name)
        self.name_label.setStyleSheet(
            f"font-size: {self.scale_mgr.pt(14)}px; font-weight: bold; color: #E0E0E0;"
        )
        top_layout.addWidget(self.name_label, 1)

        layout.addLayout(top_layout)

        # File Path
        self.path_label = QLabel(item.path)
        self.path_label.setStyleSheet(
            f"font-size: {self.scale_mgr.pt(11)}px; color: #8E9297;"
        )
        layout.addWidget(self.path_label)


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.scale_mgr = UiScaleManager.get_instance()
        self._current_worker: Optional[SearchWorker] = None
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._do_search)

        self._init_ui()

    def _init_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Main background container with rounded borders
        self.container = QWidget(self)
        self.container.setObjectName("Container")

        # Drop shadow effect
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setColor(QColor(0, 0, 0, 160))
        self.container.setGraphicsEffect(self.shadow)

        # Root layout for padding around shadow
        self.root_layout = QVBoxLayout(self)
        self.root_layout.addWidget(self.container)

        self.container_layout = QVBoxLayout(self.container)

        # Header with Search input
        self.search_box = QHBoxLayout()

        self.search_icon = QLabel("🔍")
        self.search_box.addWidget(self.search_icon)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("ZenSearch 即刻检索文件、目录或应用...")
        self.input_edit.textChanged.connect(self._on_text_changed)
        self.search_box.addWidget(self.input_edit, 1)

        self.container_layout.addLayout(self.search_box)

        # Divider line
        self.divider = QWidget()
        self.divider.setFixedHeight(1)
        self.divider.setStyleSheet("background-color: #2D313F;")
        self.container_layout.addWidget(self.divider)

        # Results list
        self.list_widget = QListWidget()
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list_widget.itemDoubleClicked.connect(self._launch_selected)
        self.container_layout.addWidget(self.list_widget)

        # Footer tips
        self.footer = QHBoxLayout()
        self.tip_text = QLabel("↑↓ 切换  |  Enter 打开  |  Ctrl+O 打开所在文件夹  |  Esc 退出")
        self.footer.addWidget(self.tip_text)
        self.container_layout.addLayout(self.footer)

        # Apply initial responsive styling & sizing
        self._apply_responsive_metrics()

    def _apply_responsive_metrics(self):
        """
        Dynamically applies responsive layout metrics, fonts, margins, and stylesheet
        based on current screen characteristics via UiScaleManager.
        """
        target_screen = self.scale_mgr.get_target_screen()
        self.scale_mgr.update_for_screen(target_screen)

        # Responsive margins and paddings
        pad_root = self.scale_mgr.dp(15)
        self.root_layout.setContentsMargins(pad_root, pad_root, pad_root, pad_root)

        pad_container = self.scale_mgr.dp(14)
        self.container_layout.setContentsMargins(pad_container, pad_container, pad_container, pad_container)
        self.container_layout.setSpacing(self.scale_mgr.dp(10))

        self.search_box.setSpacing(self.scale_mgr.dp(10))
        self.search_icon.setStyleSheet(
            f"font-size: {self.scale_mgr.pt(16)}px; color: #8E9297;"
        )

        # Drop shadow dynamic radius & offset
        self.shadow.setBlurRadius(self.scale_mgr.dp(30))
        self.shadow.setOffset(0, self.scale_mgr.dp(10))

        # Footer styling
        pad_footer_h = self.scale_mgr.dp(4)
        pad_footer_v = self.scale_mgr.dp(2)
        self.footer.setContentsMargins(pad_footer_h, pad_footer_v, pad_footer_h, pad_footer_v)
        self.tip_text.setStyleSheet(
            f"color: #6C7086; font-size: {self.scale_mgr.pt(11)}px;"
        )

        # Parametric stylesheet
        self.setStyleSheet(get_launcher_qss(self.scale_mgr))

        # Responsive window width
        calc_w = self.scale_mgr.compute_launcher_width(target_screen)
        total_w = calc_w + pad_root * 2
        self.setFixedWidth(total_w)

        self._adjust_height()

    def _adjust_height(self):
        pad_root = self.scale_mgr.dp(15)
        base_h = self.scale_mgr.dp(105)
        item_h = self.scale_mgr.dp(50)

        count = self.list_widget.count()
        if count == 0:
            self.list_widget.setFixedHeight(0)
            self.container.setFixedHeight(base_h)
            self.setFixedHeight(base_h + pad_root * 2)
        else:
            max_visible = self.scale_mgr.compute_launcher_max_visible_items()
            display_count = min(count, max_visible)
            list_h = display_count * item_h
            self.list_widget.setFixedHeight(list_h)
            self.container.setFixedHeight(list_h + base_h + self.scale_mgr.dp(10))
            self.setFixedHeight(list_h + base_h + self.scale_mgr.dp(10) + pad_root * 2)

    def _center_window(self):
        target_screen = self.scale_mgr.get_target_screen()
        self.scale_mgr.update_for_screen(target_screen)
        x, y = self.scale_mgr.compute_launcher_position(self.width(), self.height(), target_screen)
        self.move(x, y)

    def _on_text_changed(self, text: str):
        self._debounce_timer.stop()
        if not text.strip():
            self.list_widget.clear()
            self._adjust_height()
            return
        self._debounce_timer.start(35)  # 35ms debounce

    def _do_search(self):
        query = self.input_edit.text().strip()
        if not query:
            self.list_widget.clear()
            self._adjust_height()
            return

        client = EverythingClient.get_instance()
        if not client.is_available():
            self.list_widget.clear()
            item = QListWidgetItem(self.list_widget)
            from dataclasses import dataclass
            dummy_res = SearchResult(
                name="⚠️ 未连接到 Everything 检索引擎",
                path="请检查 Everything 是否已安装并运行，或右键托盘重新尝试连接",
                full_path="",
                is_folder=False
            )
            item.setData(Qt.UserRole, dummy_res)
            item_widget = ResultItemWidget(dummy_res, self.scale_mgr)
            item.setSizeHint(item_widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, item_widget)
            self._adjust_height()
            return

        if self._current_worker and self._current_worker.isRunning():
            self._current_worker.terminate()

        self._current_worker = SearchWorker(query)
        self._current_worker.results_ready.connect(self._on_results_ready)
        self._current_worker.start()

    def _on_results_ready(self, results: List[SearchResult]):
        self.list_widget.clear()
        for res in results:
            item = QListWidgetItem(self.list_widget)
            item.setData(Qt.UserRole, res)
            item_widget = ResultItemWidget(res, self.scale_mgr)
            item.setSizeHint(item_widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, item_widget)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

        self._adjust_height()

    def _get_current_selected_result(self) -> Optional[SearchResult]:
        curr = self.list_widget.currentItem()
        if curr:
            return curr.data(Qt.UserRole)
        return None

    def _launch_selected(self):
        res = self._get_current_selected_result()
        if res and os.path.exists(res.full_path):
            try:
                os.startfile(res.full_path)
            except Exception:
                pass
            self.hide()

    def _reveal_in_explorer(self):
        res = self._get_current_selected_result()
        if res and os.path.exists(res.full_path):
            try:
                subprocess.Popen(['explorer.exe', f'/select,{res.full_path}'])
            except Exception:
                pass
            self.hide()

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key_Escape:
            self.hide()
            event.accept()
        elif key == Qt.Key_Up:
            curr = self.list_widget.currentRow()
            if curr > 0:
                self.list_widget.setCurrentRow(curr - 1)
            event.accept()
        elif key == Qt.Key_Down:
            curr = self.list_widget.currentRow()
            if curr < self.list_widget.count() - 1:
                self.list_widget.setCurrentRow(curr + 1)
            event.accept()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self._launch_selected()
            event.accept()
        elif key == Qt.Key_O and (modifiers & Qt.ControlModifier):
            # Ctrl + O: Reveal file in Explorer
            self._reveal_in_explorer()
            event.accept()
        else:
            super().keyPressEvent(event)

    def changeEvent(self, event):
        if event.type() == event.ActivationChange:
            if not self.isActiveWindow():
                self.hide()
        super().changeEvent(event)

    def nativeEvent(self, eventType, message):
        """
        Catches Windows display change and DPI change messages for dynamic self-healing.
        WM_DISPLAYCHANGE = 0x007E, WM_DPICHANGED = 0x02E0
        """
        msg = wintypes.MSG.from_address(message.__int__())
        if msg.message in (0x007E, 0x02E0):
            self._apply_responsive_metrics()
            self._center_window()
        return super().nativeEvent(eventType, message)

    def show_and_activate(self, force_show: bool = False):
        # Toggle: if already visible and active, hide it (unless forced to show by secondary instance activation)
        if not force_show and self.isVisible() and self.isActiveWindow():
            self.hide()
            return

        # 唤醒时清空搜索栏与搜索结果，绝不保留上一次历史记录 (Clean Slate)
        self._debounce_timer.stop()
        if self._current_worker and self._current_worker.isRunning():
            try:
                self._current_worker.terminate()
            except Exception:
                pass
            self._current_worker = None
        self.input_edit.clear()
        self.list_widget.clear()

        # Dynamically recalculate scale and layout for the current target screen
        self._apply_responsive_metrics()
        self._center_window()
        self.show()
        self.raise_()

        # Connect screenChanged on window handle if available
        win_handle = self.windowHandle()
        if win_handle:
            try:
                win_handle.screenChanged.connect(self._on_screen_changed)
            except Exception:
                pass

        # Force foreground activation across process boundaries
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(self.winId())
            fg_hwnd = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
            cur_tid = kernel32.GetCurrentThreadId()

            if fg_tid and fg_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, fg_tid, True)
                user32.BringWindowToTop(hwnd)
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
                user32.AttachThreadInput(cur_tid, fg_tid, False)
            else:
                user32.BringWindowToTop(hwnd)
                user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

        self.activateWindow()
        self.input_edit.setFocus()

    def _on_screen_changed(self, new_screen):
        self._apply_responsive_metrics()
        self._center_window()
