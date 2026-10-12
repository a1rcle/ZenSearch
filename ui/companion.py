"""
ZenSearch - Explorer Companion (Type-to-Search) Floating Bar
Docks seamlessly to the active Windows Explorer window.
Multi-DPI & Responsive UI adapted based on ZenSearch技术方案.md:
- Dynamic sizing via UiScaleManager & parametric QSS
- Proportional width scaling based on attached Explorer window: W_comp = clamp(400, int(W_exp * 0.52), 640)
- Screen boundary clamping (safe against screen edges and taskbar collision)
- Native display and DPI change message handling
"""

import os
import subprocess
from typing import List, Optional
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QPoint
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QLabel, QGraphicsDropShadowEffect, QApplication
)

import ctypes
from ctypes import wintypes
import win32gui
from core.everything_client import EverythingClient, SearchResult
from core.explorer import ExplorerManager
from ui.scale_manager import UiScaleManager, get_companion_qss


class ScopedSearchWorker(QThread):
    results_ready = pyqtSignal(list)

    def __init__(self, folder: str, query: str, max_results: int = 30):
        super().__init__()
        self.folder = folder
        self.query = query
        self.max_results = max_results
        self.client = EverythingClient.get_instance()

    def run(self):
        results = self.client.search_scoped(self.folder, self.query, max_results=self.max_results)
        self.results_ready.emit(results)


class CompanionItemWidget(QWidget):
    def __init__(self, item: SearchResult, scale_mgr: UiScaleManager, parent=None):
        super().__init__(parent)
        self.scale_mgr = scale_mgr

        layout = QVBoxLayout(self)
        pad_h = self.scale_mgr.dp(8)
        pad_v = self.scale_mgr.dp(4)
        layout.setContentsMargins(pad_h, pad_v, pad_h, pad_v)
        layout.setSpacing(self.scale_mgr.dp(1))

        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(self.scale_mgr.dp(6))

        icon_label = QLabel("📁" if item.is_folder else "📄")
        icon_label.setStyleSheet(f"font-size: {self.scale_mgr.pt(13)}px;")
        top_layout.addWidget(icon_label)

        self.name_label = QLabel(item.name)
        self.name_label.setStyleSheet(
            f"font-size: {self.scale_mgr.pt(13)}px; font-weight: bold; color: #E0E0E0;"
        )
        top_layout.addWidget(self.name_label, 1)

        layout.addLayout(top_layout)

        self.path_label = QLabel(item.path)
        self.path_label.setStyleSheet(
            f"font-size: {self.scale_mgr.pt(10)}px; color: #8E9297;"
        )
        layout.addWidget(self.path_label)


class CompanionBar(QWidget):
    def __init__(self):
        super().__init__()
        self.scale_mgr = UiScaleManager.get_instance()
        self.explorer_mgr = ExplorerManager.get_instance()
        self._target_explorer_hwnd: Optional[int] = None
        self._current_folder: Optional[str] = None
        self._worker: Optional[ScopedSearchWorker] = None

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._do_scoped_search)

        self._init_ui()

    def _init_ui(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.container = QWidget(self)
        self.container.setObjectName("CompanionContainer")

        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setColor(QColor(0, 0, 0, 180))
        self.container.setGraphicsEffect(self.shadow)

        self.root_layout = QVBoxLayout(self)
        self.root_layout.addWidget(self.container)

        self.container_layout = QVBoxLayout(self.container)

        # Scope badge header
        self.scope_label = QLabel("📂 当前目录筛选")
        self.container_layout.addWidget(self.scope_label)

        # Input box
        self.input_layout = QHBoxLayout()

        self.search_icon = QLabel("⚡")
        self.input_layout.addWidget(self.search_icon)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("过滤当前目录及子目录...")
        self.input_edit.textChanged.connect(self._on_text_changed)
        self.input_layout.addWidget(self.input_edit, 1)

        self.container_layout.addLayout(self.input_layout)

        self.divider = QWidget()
        self.divider.setFixedHeight(1)
        self.divider.setStyleSheet("background-color: #282C34;")
        self.container_layout.addWidget(self.divider)

        # Results list
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._launch_selected)
        self.container_layout.addWidget(self.list_widget)

        self._apply_responsive_metrics()

    def _apply_responsive_metrics(self, explorer_w: Optional[int] = None):
        """
        Dynamically applies responsive layout metrics, margins, and parametric stylesheet.
        """
        target_screen = self.scale_mgr.get_target_screen(self._target_explorer_hwnd)
        self.scale_mgr.update_for_screen(target_screen)

        pad_root = self.scale_mgr.dp(12)
        self.root_layout.setContentsMargins(pad_root, pad_root, pad_root, pad_root)

        pad_container = self.scale_mgr.dp(10)
        self.container_layout.setContentsMargins(pad_container, pad_container, pad_container, pad_container)
        self.container_layout.setSpacing(self.scale_mgr.dp(6))

        self.scope_label.setStyleSheet(
            f"font-size: {self.scale_mgr.pt(11)}px; color: #61AFEF; font-weight: bold;"
        )

        self.input_layout.setSpacing(self.scale_mgr.dp(8))
        self.search_icon.setStyleSheet(f"font-size: {self.scale_mgr.pt(13)}px;")

        self.shadow.setBlurRadius(self.scale_mgr.dp(24))
        self.shadow.setOffset(0, self.scale_mgr.dp(6))

        self.setStyleSheet(get_companion_qss(self.scale_mgr))

        # Compute responsive width based on attached Explorer window (if available)
        if explorer_w and explorer_w > 0:
            calc_w = self.scale_mgr.compute_companion_width(explorer_w)
        else:
            calc_w = 540

        total_w = calc_w + pad_root * 2
        self.setFixedWidth(total_w)
        self._adjust_height()

    def _adjust_height(self):
        pad_root = self.scale_mgr.dp(12)
        base_h = self.scale_mgr.dp(85)
        item_h = self.scale_mgr.dp(46)

        count = self.list_widget.count()
        if count == 0:
            self.list_widget.setFixedHeight(0)
            self.container.setFixedHeight(base_h)
            self.setFixedHeight(base_h + pad_root * 2)
        else:
            display_count = min(count, 6)
            list_h = display_count * item_h
            self.list_widget.setFixedHeight(list_h)
            self.container.setFixedHeight(list_h + base_h + self.scale_mgr.dp(10))
            self.setFixedHeight(list_h + base_h + self.scale_mgr.dp(10) + pad_root * 2)

    def attach_and_show(self, initial_char: str = ""):
        """
        Calculates active Explorer position and docks to bottom-right corner with
        display workarea boundary clamping.
        """
        fg_hwnd = win32gui.GetForegroundWindow()
        if not self.explorer_mgr.is_explorer_window(fg_hwnd):
            return

        self._target_explorer_hwnd = fg_hwnd
        folder_path = self.explorer_mgr.get_explorer_path(fg_hwnd)
        if not folder_path:
            return

        self._current_folder = folder_path
        folder_name = os.path.basename(folder_path.rstrip('\\/')) or folder_path
        self.scope_label.setText(f"📂 当前目录: {folder_name}")

        rect = self.explorer_mgr.get_window_rect(fg_hwnd)
        exp_w = (rect[2] - rect[0]) if rect else 800

        # Update metrics with current Explorer window size
        self._apply_responsive_metrics(explorer_w=exp_w)

        if rect:
            target_x, target_y = self.scale_mgr.compute_companion_position(
                exp_rect=rect,
                comp_w=self.width(),
                comp_h=self.height()
            )
            self.move(target_x, target_y)

        self.input_edit.setText(initial_char)
        self.show()
        self.raise_()

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
                user32.ShowWindow(hwnd, 5)
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
        if initial_char:
            self.input_edit.setCursorPosition(len(initial_char))

    def _on_text_changed(self, text: str):
        self._debounce_timer.stop()
        if not text.strip():
            self.list_widget.clear()
            self._adjust_height()
            return
        self._debounce_timer.start(35)

    def _do_scoped_search(self):
        query = self.input_edit.text().strip()
        if not self._current_folder or not query:
            self.list_widget.clear()
            self._adjust_height()
            return

        client = EverythingClient.get_instance()
        if not client.is_available():
            self.list_widget.clear()
            item = QListWidgetItem(self.list_widget)
            dummy_res = SearchResult(
                name="⚠️ 未连接到 Everything 检索引擎",
                path="随行搜索需要 Everything 支持，请启动 Everything",
                full_path="",
                is_folder=False
            )
            item.setData(Qt.UserRole, dummy_res)
            item_widget = CompanionItemWidget(dummy_res, self.scale_mgr)
            item.setSizeHint(item_widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, item_widget)
            self._adjust_height()
            return

        if self._worker and self._worker.isRunning():
            self._worker.terminate()

        self._worker = ScopedSearchWorker(self._current_folder, query)
        self._worker.results_ready.connect(self._on_results_ready)
        self._worker.start()

    def _on_results_ready(self, results: List[SearchResult]):
        self.list_widget.clear()
        for res in results:
            item = QListWidgetItem(self.list_widget)
            item.setData(Qt.UserRole, res)
            item_widget = CompanionItemWidget(res, self.scale_mgr)
            item.setSizeHint(item_widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, item_widget)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

        self._adjust_height()

    def _get_current_selected(self) -> Optional[SearchResult]:
        curr = self.list_widget.currentItem()
        if curr:
            return curr.data(Qt.UserRole)
        return None

    def _launch_selected(self):
        res = self._get_current_selected()
        if res and os.path.exists(res.full_path):
            try:
                os.startfile(res.full_path)
            except Exception:
                pass
            self.hide()

    def _reveal_in_explorer(self):
        res = self._get_current_selected()
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
            # Return focus to Explorer
            if self._target_explorer_hwnd and win32gui.IsWindow(self._target_explorer_hwnd):
                try:
                    win32gui.SetForegroundWindow(self._target_explorer_hwnd)
                except Exception:
                    pass
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
            self._reveal_in_explorer()
            event.accept()
        else:
            super().keyPressEvent(event)

    def nativeEvent(self, eventType, message):
        """
        Catches Windows display change and DPI change messages.
        """
        msg = wintypes.MSG.from_address(message.__int__())
        if msg.message in (0x007E, 0x02E0):
            if self._target_explorer_hwnd and win32gui.IsWindow(self._target_explorer_hwnd):
                rect = self.explorer_mgr.get_window_rect(self._target_explorer_hwnd)
                exp_w = (rect[2] - rect[0]) if rect else 800
            else:
                exp_w = 800
            self._apply_responsive_metrics(explorer_w=exp_w)
        return super().nativeEvent(eventType, message)
