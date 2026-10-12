"""
ZenSearch - Main Application Entry Point
Coordinates Global Search Launcher, Explorer Companion, Path Injector, and Keyboard Hooks.
"""

import sys
import os
from typing import Optional
from PyQt5.QtCore import QObject, pyqtSignal, Qt
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox

# Add project root to sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import argparse
from core.logger import setup_logger, logger
from core.everything_client import EverythingClient
from core.everything_probe import EverythingProbe
from core.autostart import AutoStartManager
from core.hotkey import HotkeyManager
from core.injector import PathInjector
from core.single_instance import SingleInstanceManager, MessageHostWindow
from ui.scale_manager import init_high_dpi_context
from ui.launcher import LauncherWindow
from ui.companion import CompanionBar
from ui.guide_dialog import EverythingGuideDialog


class BridgeSignals(QObject):
    double_ctrl = pyqtSignal()
    quick_switch = pyqtSignal()
    auto_switch = pyqtSignal(int)
    explorer_type = pyqtSignal(str)


def create_tray_icon_pixmap() -> QPixmap:
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor('#4C566A'))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(2, 2, 28, 28, 6, 6)
    painter.setPen(QColor('#ECEFF4'))
    font = QFont('Arial', 14, QFont.Bold)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignCenter, 'Z')
    painter.end()
    return pix


class ZenSearchApp:
    def __init__(self, single_inst: Optional[SingleInstanceManager] = None, debug_mode: bool = False, silent_mode: bool = False):
        self.single_inst = single_inst
        self.debug_mode = debug_mode
        self.silent_mode = silent_mode
        setup_logger(debug_mode=debug_mode)
        logger.info(f"ZenSearch 启动中... (Debug: {debug_mode}, Silent: {silent_mode})")

        # 必须在创建 QApplication 实例前向系统声明 Per-Monitor DPI Aware V2
        init_high_dpi_context()

        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # 注册跨实例通信与唤醒消息宿主窗口 (Title: ZenSearch_MessageHost)
        self.message_host = MessageHostWindow()

        # 自启动配置有效性自检与自愈
        healed, heal_msg = AutoStartManager.self_heal_if_needed()
        if healed:
            logger.info(f"[AutoStart] 自启动项已自愈: {heal_msg}")

        # Everything 环境四级探针自检与联动拉起
        ready, probe_info = EverythingProbe.ensure_everything_running()
        if ready:
            logger.info(f"Everything 引擎就绪: {probe_info.details}")
        else:
            if not probe_info.installed:
                logger.warning("未检测到 Everything 安装，准备弹出友好引导窗口...")
                # 在非静默模式下向用户展示引导窗口，或开机静默启动时允许安全降级
                if not self.silent_mode:
                    guide = EverythingGuideDialog()
                    guide.exec_()
                else:
                    logger.info("开机静默启动模式：自动进入安全降级模式 (不弹窗打扰)")
            else:
                logger.warning(f"Everything 已安装但自动拉起或 IPC 连接超时: {probe_info.details}")

        self.everything_client = EverythingClient.get_instance()

        self.signals = BridgeSignals()
        self.launcher = LauncherWindow()
        self.companion = CompanionBar()
        self.injector = PathInjector()

        # Connect signals across threads safely
        self.signals.double_ctrl.connect(self.launcher.show_and_activate)
        self.signals.quick_switch.connect(self.injector.quick_switch)
        self.signals.auto_switch.connect(self.injector.auto_switch)
        self.signals.explorer_type.connect(self.companion.attach_and_show)
        self.message_host.wakeup_triggered.connect(lambda: self.launcher.show_and_activate(force_show=True))

        # Initialize hotkey manager & tracking
        self.hotkey_mgr = HotkeyManager(
            on_double_ctrl=lambda: self.signals.double_ctrl.emit(),
            on_quick_switch=lambda: self.signals.quick_switch.emit(),
            on_explorer_type=lambda char: self.signals.explorer_type.emit(char),
            on_auto_switch=lambda h_dlg: self.signals.auto_switch.emit(h_dlg)
        )
        self.hotkey_mgr.start()

        self._init_tray()

    def _init_tray(self):
        self.tray = QSystemTrayIcon(self.app)
        self.tray.setIcon(QIcon(create_tray_icon_pixmap()))
        self.tray.setToolTip("ZenSearch - 类似 Listary 的极速搜索增强器")

        menu = QMenu()

        act_show = QAction("🔍 打开全局搜索 (双击 Ctrl)", menu)
        act_show.triggered.connect(self.launcher.show_and_activate)
        menu.addAction(act_show)

        act_switch = QAction("⚡ 路径注入 Quick Switch (Ctrl + G)", menu)
        act_switch.triggered.connect(self.injector.quick_switch)
        menu.addAction(act_switch)

        menu.addSeparator()

        # 开机自启动复选选项
        self.act_autostart = QAction("🚀 开机自启动", menu, checkable=True)
        self.act_autostart.setChecked(AutoStartManager.is_autostart_enabled())
        self.act_autostart.triggered.connect(self._toggle_autostart)
        menu.addAction(self.act_autostart)

        # Everything 重新检测/拉起选项
        act_recheck_everything = QAction("🔄 检查 Everything 连接状态", menu)
        act_recheck_everything.triggered.connect(self._recheck_everything)
        menu.addAction(act_recheck_everything)

        menu.addSeparator()

        act_about = QAction("ℹ️ 关于 ZenSearch", menu)
        act_about.triggered.connect(self._show_about)
        menu.addAction(act_about)

        act_quit = QAction("❌ 退出", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        # Startup notification balloon (静默自启时不弹气泡打扰用户)
        if not self.silent_mode:
            self.tray.showMessage(
                "ZenSearch 已启动",
                "• 双击 Ctrl 呼出全局即时搜索\n• 资源管理器中直接打字随行过滤\n• 文件对话框按 Ctrl+G 瞬间注入路径",
                QSystemTrayIcon.Information,
                3000
            )

    def _toggle_autostart(self, checked: bool):
        success = AutoStartManager.set_autostart(checked, silent=True)
        if success:
            state_text = "已开启" if checked else "已关闭"
            self.tray.showMessage(
                "开机自启动设置更新",
                f"ZenSearch 开机自启动{state_text} (免提权 HKCU 配置)",
                QSystemTrayIcon.Information,
                2000
            )
        else:
            self.act_autostart.setChecked(not checked)
            QMessageBox.warning(None, "设置失败", "无法修改开机自启动配置，请检查注册表写入权限。")

    def _recheck_everything(self):
        ready, probe = EverythingProbe.ensure_everything_running()
        if ready:
            self.tray.showMessage(
                "Everything 连通成功",
                f"Everything 检索引擎正常工作中 ({probe.details})",
                QSystemTrayIcon.Information,
                2500
            )
        else:
            if not probe.installed:
                guide = EverythingGuideDialog()
                guide.exec_()
            else:
                QMessageBox.warning(
                    None,
                    "Everything 连接提示",
                    f"未能连接至 Everything IPC 服务：\n{probe.details}\n\n请尝试手动运行 Everything。"
                )

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.launcher.show_and_activate()

    def _show_about(self):
        autostart_str = "已开启" if AutoStartManager.is_autostart_enabled() else "未开启"
        ev_ready = "已连接" if self.everything_client.is_available() else "未连接 (降级模式)"
        QMessageBox.information(
            None,
            "关于 ZenSearch",
            "ZenSearch v1.2\n\n"
            "• 毫秒级全盘即时搜索 (Everything IPC 深度集成)\n"
            "• 双击 Ctrl 唤醒全局浮动栏 (Up/Down选择, Ctrl+O定位)\n"
            "• 路径注入 (Quick Switch: Ctrl+G 对话框瞬间重定向)\n"
            "• 资源管理器随行增强 (直接键入即刻筛选)\n"
            f"• Everything 引擎状态: {ev_ready}\n"
            f"• 开机自启动: {autostart_str}\n"
        )

    def _quit(self):
        self.hotkey_mgr.stop()
        self.everything_client.close()
        if hasattr(self, 'message_host') and self.message_host:
            self.message_host.close()
        if hasattr(self, 'single_inst') and self.single_inst:
            self.single_inst.release()
        self.app.quit()

    def run(self):
        try:
            return self.app.exec_()
        finally:
            self.hotkey_mgr.stop()
            self.everything_client.close()
            if hasattr(self, 'message_host') and self.message_host:
                self.message_host.close()
            if hasattr(self, 'single_inst') and self.single_inst:
                self.single_inst.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="ZenSearch 启动器")
    parser.add_argument("--debug", action="store_true", help="开启详细调试日志")
    parser.add_argument("--silent", action="store_true", help="静默后台自启模式 (不弹窗、不闪烁控制台)")
    args = parser.parse_args()

    # 单实例管控最前置拦截 (在任何 GUI/COM/系统钩子初始化之前执行)
    single_inst = SingleInstanceManager()
    if not single_inst.acquire():
        if args.silent:
            # 静默启动幂等性：无感秒退
            sys.exit(0)
        else:
            # 二次启动交互唤醒：通知已有后台实例呼出主界面并置顶
            activated = single_inst.activate_existing_instance()
            if not activated:
                # 托盘气泡/弹窗防呆兜底提示
                SingleInstanceManager.show_already_running_fallback()
            sys.exit(0)

    zen = ZenSearchApp(single_inst=single_inst, debug_mode=args.debug, silent_mode=args.silent)
    sys.exit(zen.run())
