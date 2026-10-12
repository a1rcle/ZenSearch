"""
ZenSearch - Everything Missing Guide Dialog
Provides a modern, high-quality prompt window when Everything is not detected.
Offers:
1. One-click button to launch official voidtools download website
2. "Install Later" button to enter graceful degradation mode safely without crashing
"""

import sys
import webbrowser
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QColor
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGraphicsDropShadowEffect, QWidget
)

from ui.scale_manager import UiScaleManager


class EverythingGuideDialog(QDialog):
    """
    Modern dark-themed guide dialog informing the user about the Everything dependency.
    """

    DOWNLOAD_URL = "https://www.voidtools.com/zh-cn/"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scale_mgr = UiScaleManager.get_instance()
        self._action_taken = "later"  # "download" or "later"
        self._init_ui()

    def _init_ui(self):
        self.setWindowTitle("ZenSearch - 依赖环境提示")
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog {
                background-color: #1E1F29;
                color: #ECEFF4;
            }
            QLabel {
                color: #D8DEE9;
            }
            QPushButton#DownloadBtn {
                background-color: #5E81AC;
                color: #ECEFF4;
                border: none;
                border-radius: 6px;
                font-weight: bold;
                padding: 8px 18px;
            }
            QPushButton#DownloadBtn:hover {
                background-color: #81A1C1;
            }
            QPushButton#DownloadBtn:pressed {
                background-color: #4C566A;
            }
            QPushButton#LaterBtn {
                background-color: #3B4252;
                color: #D8DEE9;
                border: 1px solid #4C566A;
                border-radius: 6px;
                padding: 8px 18px;
            }
            QPushButton#LaterBtn:hover {
                background-color: #434C5E;
                color: #ECEFF4;
            }
        """)

        layout = QVBoxLayout(self)
        pad = self.scale_mgr.dp(20)
        layout.setContentsMargins(pad, pad, pad, pad)
        layout.setSpacing(self.scale_mgr.dp(16))

        # Header Title
        title_box = QHBoxLayout()
        icon_label = QLabel("🔍")
        icon_label.setStyleSheet(f"font-size: {self.scale_mgr.pt(24)}px;")
        title_box.addWidget(icon_label)

        title_text = QLabel("未检测到 Everything 检索引擎")
        title_text.setStyleSheet(f"font-size: {self.scale_mgr.pt(16)}px; font-weight: bold; color: #ECEFF4;")
        title_box.addWidget(title_text, 1)
        layout.addLayout(title_box)

        # Description text
        desc_label = QLabel(
            "ZenSearch 的全盘毫秒级极速搜索依赖于 <b>Everything</b> 提供的 IPC 索引服务。<br><br>"
            "检测到您的计算机上尚未安装 Everything，或者未加入系统标准路径。<br>"
            "推荐前往官网下载并安装 Everything（建议勾选“作为服务安装”与“随系统自启”），即可畅享极致即时检索体验。"
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(f"font-size: {self.scale_mgr.pt(12)}px; line-height: 1.5; color: #D8DEE9;")
        layout.addWidget(desc_label)

        # Tip note
        note_label = QLabel(
            "💡 <i>提示：若选择稍后安装，ZenSearch 将进入<b>安全降级模式</b>，路径注入 (Quick Switch) 功能仍可正常使用。</i>"
        )
        note_label.setWordWrap(True)
        note_label.setStyleSheet(f"font-size: {self.scale_mgr.pt(10)}px; color: #EBCB8B;")
        layout.addWidget(note_label)

        # Button row
        btn_box = QHBoxLayout()
        btn_box.addStretch(1)

        self.btn_later = QPushButton("稍后安装 (降级模式)")
        self.btn_later.setObjectName("LaterBtn")
        self.btn_later.setCursor(Qt.PointingHandCursor)
        self.btn_later.clicked.connect(self._on_later_clicked)
        btn_box.addWidget(self.btn_later)

        self.btn_download = QPushButton("前往官网下载 Everything")
        self.btn_download.setObjectName("DownloadBtn")
        self.btn_download.setCursor(Qt.PointingHandCursor)
        self.btn_download.clicked.connect(self._on_download_clicked)
        btn_box.addWidget(self.btn_download)

        layout.addLayout(btn_box)

        self.setFixedWidth(self.scale_mgr.dp(480))
        self.adjustSize()

    def _on_download_clicked(self):
        self._action_taken = "download"
        try:
            webbrowser.open(self.DOWNLOAD_URL)
        except Exception:
            pass
        self.accept()

    def _on_later_clicked(self):
        self._action_taken = "later"
        self.reject()

    @property
    def action_taken(self) -> str:
        return self._action_taken
