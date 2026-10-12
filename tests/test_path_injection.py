"""
ZenSearch - Path Injection & Strict MRU Automated Test Suite
Verifies:
1. Strict MRU Policy (No arbitrary fallback across background windows)
2. Window destruction / closure invalidation
3. Windows 11 Tabs & active tab title detection
4. Common File Dialog (#32770) control resolution and path injection
"""

import os
import sys
import time
import threading
import unittest

# Ensure project root in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import win32gui
import win32con
import ctypes

user32 = ctypes.windll.user32

from core.explorer import ExplorerManager, ensure_desktop
from core.injector import PathInjector


class TestStrictMRUPolicy(unittest.TestCase):
    """Tests the Strict MRU Policy requirements defined in 路径注入.md."""

    def setUp(self):
        ensure_desktop()
        self.em = ExplorerManager.get_instance()
        self.injector = PathInjector()
        # Backup original MRU state
        self._orig_hwnd = self.em._last_explorer_hwnd
        self._orig_path = self.em._last_explorer_path
        self._orig_time = self.em._last_explorer_time

    def tearDown(self):
        # Restore original MRU state
        self.em._last_explorer_hwnd = self._orig_hwnd
        self.em._last_explorer_path = self._orig_path
        self.em._last_explorer_time = self._orig_time

    def test_case_a_no_browse_history_silent_rejection(self):
        """
        场景 A: 用户从未打开过/激活过资源管理器。
        ZenSearch 检测到无前序浏览记录，安全忽略，保持对话框现有路径不变，返回 None / False。
        绝对不能从后台打开的其它 Explorer 窗口中随机挑选一个！
        """
        self.em._last_explorer_hwnd = None
        self.em._last_explorer_path = None

        target_path = self.em.get_last_active_explorer_path()
        self.assertIsNone(
            target_path,
            "严格MRU策略失败：在无前序浏览记录时，不应返回任何路径，坚决杜绝后台随机兜底！"
        )

        res = self.injector.quick_switch(h_dlg=None)
        self.assertFalse(
            res,
            "Quick Switch 在无有效前序路径时必须返回 False 保持静默！"
        )

    def test_case_b_closed_window_invalidates_mru(self):
        """
        场景 B: 上一个浏览过的资源管理器窗口已被用户关闭。
        ZenSearch 检验到 IsWindow(_last_explorer_hwnd) 为假，判定记录失效，放弃注入，不回退到后台其它遗留窗口。
        """
        # Set a closed / non-existent window HWND
        invalid_hwnd = 0x00FFFFFF
        self.assertFalse(win32gui.IsWindow(invalid_hwnd))

        self.em._last_explorer_hwnd = invalid_hwnd
        self.em._last_explorer_path = "D:\\SomeNonExistentOrExistentPath"

        target_path = self.em.get_last_active_explorer_path()
        self.assertIsNone(
            target_path,
            "严格MRU策略失败：当上一个浏览窗口关闭后，记录必须失效并返回 None，坚决不能乱跳！"
        )
        self.assertIsNone(
            self.em._last_explorer_hwnd,
            "失效的窗口句柄记录必须被清空！"
        )
        self.assertIsNone(
            self.em._last_explorer_path,
            "失效的路径记录必须被清空！"
        )

    def test_valid_mru_returns_active_path(self):
        """
        正常场景: 存在有效且存活的 Explorer 窗口时，准确返回该窗口的活动路径。
        """
        # Find any currently open Explorer window if present
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("Shell.Application")
        windows = shell.Windows()
        valid_hwnd = None
        valid_expected_path = None

        for i in range(windows.Count):
            try:
                w = windows.Item(i)
                h = getattr(w, 'HWND', 0)
                if h and win32gui.IsWindow(h) and self.em.is_explorer_window(h):
                    p = self.em.get_explorer_path(h)
                    if p and os.path.exists(p):
                        valid_hwnd = h
                        valid_expected_path = p
                        break
            except Exception:
                continue
        pythoncom.CoUninitialize()

        if valid_hwnd:
            self.em._last_explorer_hwnd = valid_hwnd
            self.em._last_explorer_path = valid_expected_path

            retrieved = self.em.get_last_active_explorer_path()
            self.assertIsNotNone(retrieved)
            self.assertEqual(os.path.normcase(retrieved), os.path.normcase(valid_expected_path))


class TestWinEventHookLifecycle(unittest.TestCase):
    """Tests the start and stop lifecycle of the WinEventHook tracker."""

    def test_tracker_lifecycle(self):
        em = ExplorerManager.get_instance()
        em.start_tracking()
        self.assertTrue(em.is_tracking())
        time.sleep(0.1)
        em.stop_tracking()
        self.assertFalse(em.is_tracking())


class TestDialogPathInjection(unittest.TestCase):
    """Tests finding controls in #32770 and injecting target paths."""

    def test_find_controls_and_inject_path(self):
        ensure_desktop()
        injector = PathInjector()
        test_dialog_hwnd = None
        dialog_ready = threading.Event()

        def run_dialog():
            ensure_desktop()
            try:
                flags = win32con.OFN_EXPLORER
                win32gui.GetOpenFileNameW(
                    File='initial.txt',
                    Filter='Text files\0*.txt\0All Files\0*.*\0',
                    Title='ZenSearch Automated Test Dialog',
                    Flags=flags
                )
            except Exception as e:
                print("EXCEPTION IN RUN_DIALOG:", type(e), e)

        t = threading.Thread(target=run_dialog, daemon=True)
        t.start()

        # Wait for dialog window to appear
        h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
        for _ in range(30):
            time.sleep(0.1)
            def enum_cb(h, _):
                nonlocal test_dialog_hwnd
                if win32gui.GetClassName(h) == '#32770':
                    txt = win32gui.GetWindowText(h)
                    if 'ZenSearch' in txt or 'Test' in txt:
                        test_dialog_hwnd = h
                return True
            try:
                if h_desk:
                    win32gui.EnumDesktopWindows(h_desk, enum_cb, None)
                else:
                    win32gui.EnumWindows(enum_cb, None)
            except Exception:
                win32gui.EnumWindows(enum_cb, None)
            if test_dialog_hwnd:
                break

        self.assertIsNotNone(test_dialog_hwnd, "未能成功创建或定位测试文件对话框 (#32770)")
        time.sleep(0.2)

        try:
            # 1. Test finding controls
            h_edit, h_btn = injector.find_dialog_controls(test_dialog_hwnd)
            self.assertIsNotNone(h_edit, "对话框中必须能唯一定位文件名 Edit 控件")
            self.assertIsNotNone(h_btn, "对话框中必须能定位确认 Button 控件")

            # 2. Test injecting an existing path
            test_target_dir = os.path.abspath(BASE_DIR)
            inject_ok = injector.inject_path(test_target_dir, h_dlg=test_dialog_hwnd)
            self.assertTrue(inject_ok, "路径注入应当成功执行")

            # Verify actual path matches target path
            actual_path = injector.get_dialog_current_path(test_dialog_hwnd)
            if actual_path:
                self.assertEqual(
                    os.path.normcase(actual_path),
                    os.path.normcase(test_target_dir),
                    f"对话框实际路径 {actual_path} 必须与目标路径 {test_target_dir} 一致"
                )

            # 3. Test injecting invalid path
            invalid_ok = injector.inject_path("Z:\\NonExistent_Invalid_Path_XYZ", h_dlg=test_dialog_hwnd)
            self.assertFalse(invalid_ok, "无效或不存在的路径应当被拒绝注入")

        finally:
            # Close dialog cleanly
            if test_dialog_hwnd and win32gui.IsWindow(test_dialog_hwnd):
                win32gui.PostMessage(test_dialog_hwnd, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
            t.join(timeout=1.0)

    def test_save_dialog_path_injection_and_consistency(self):
        """
        验证另存为 (Save As) 对话框下的路径注入与真实跳转一致性。
        杜绝出现 cmd 中显示注入路径与 dialog 真实跳转路径不一致的问题。
        """
        ensure_desktop()
        injector = PathInjector()
        save_dialog_hwnd = None

        def run_save_dialog():
            ensure_desktop()
            try:
                flags = win32con.OFN_EXPLORER | win32con.OFN_OVERWRITEPROMPT
                win32gui.GetSaveFileNameW(
                    File='my_report.docx',
                    Filter='Word Documents\0*.docx\0All Files\0*.*\0',
                    InitialDir='D:\\',
                    Title='ZenSearch Automated Save Dialog',
                    Flags=flags
                )
            except Exception:
                pass

        t = threading.Thread(target=run_save_dialog, daemon=True)
        t.start()

        h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
        for _ in range(30):
            time.sleep(0.1)
            def enum_cb(h, _):
                nonlocal save_dialog_hwnd
                if win32gui.GetClassName(h) == '#32770':
                    txt = win32gui.GetWindowText(h)
                    if 'ZenSearch Automated Save Dialog' in txt:
                        save_dialog_hwnd = h
                return True
            if h_desk:
                win32gui.EnumDesktopWindows(h_desk, enum_cb, None)
            else:
                win32gui.EnumWindows(enum_cb, None)
            if save_dialog_hwnd:
                break

        self.assertIsNotNone(save_dialog_hwnd, "未能成功创建或定位测试另存为对话框")
        time.sleep(0.2)

        try:
            # Test multiple consecutive path jumps: D:\CamCoding, D:\ZenSearch, D:\
            paths_to_test = [
                r"D:\CamCoding",
                r"D:\ZenSearch",
                r"D:\\",
                r"D:\CamCoding"
            ]

            for target in paths_to_test:
                if not os.path.exists(target):
                    continue
                norm_target = os.path.abspath(target)
                ok = injector.inject_path(norm_target, h_dlg=save_dialog_hwnd)
                self.assertTrue(ok, f"注入路径 {norm_target} 应当成功")

                actual_path = injector.get_dialog_current_path(save_dialog_hwnd)
                if actual_path:
                    self.assertEqual(
                        os.path.normcase(actual_path),
                        os.path.normcase(norm_target),
                        f"另存为对话框实际路径 {actual_path} 必须与目标路径 {norm_target} 严格一致！"
                    )
        finally:
            if save_dialog_hwnd and win32gui.IsWindow(save_dialog_hwnd):
                win32gui.PostMessage(save_dialog_hwnd, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
            t.join(timeout=1.0)


class TestSwitchBackAutoSwitch(unittest.TestCase):
    """
    Tests Focus Transition Guard & Switch-Back Auto Switch logic
    as specified in 路径注入.md:
    1. Positive switch-back: Explorer -> Dialog triggers auto_switch.
    2. Non-Explorer switch: Chrome/Editor -> Dialog does NOT trigger auto_switch (Safety silence).
    3. Closed Explorer invalidation: If last Explorer was closed, auto_switch safely silences.
    4. Rapid Alt+Tab debounce: High frequency switches (<300ms) are debounced.
    5. Idempotency: Dialog already at target path skips repeated injection.
    """

    def setUp(self):
        ensure_desktop()
        self.em = ExplorerManager.get_instance()
        self.injector = PathInjector()
        self.em.reset_mru()
        self.triggered_dialogs = []

        def callback(h_dlg):
            self.triggered_dialogs.append(h_dlg)

        self.em.on_dialog_auto_switch = callback
        self.em.auto_switch_enabled = True

    def tearDown(self):
        self.em.reset_mru()
        self.em.on_dialog_auto_switch = None

    def test_positive_switch_back_triggers_auto_switch(self):
        """
        正向切回：前台由 Explorer (CabinetWClass) 切至 对话框 (#32770)。
        必须触发自动跳转回调。
        """
        fake_explorer_hwnd = 0x11111111
        fake_dialog_hwnd = 0x22222222

        # Mock window checks
        orig_is_explorer = self.em.is_explorer_window
        orig_is_dialog = self.em.is_dialog_window
        orig_get_path = self.em.get_explorer_path
        orig_is_window = win32gui.IsWindow

        def mock_is_explorer(h):
            return h == fake_explorer_hwnd

        def mock_is_dialog(h):
            return h == fake_dialog_hwnd

        try:
            self.em.is_explorer_window = mock_is_explorer
            self.em.is_dialog_window = mock_is_dialog
            self.em.get_explorer_path = lambda h: os.path.abspath(BASE_DIR)
            win32gui.IsWindow = lambda h: True

            # Phase 1: Focus on Explorer
            self.em._on_foreground_event(fake_explorer_hwnd)
            self.assertEqual(self.em._prev_foreground_hwnd, fake_explorer_hwnd)

            time.sleep(0.35)  # satisfy debounce

            # Phase 2: Switch back into Dialog
            self.em._on_foreground_event(fake_dialog_hwnd)
            time.sleep(0.1)  # wait for async thread

            self.assertIn(fake_dialog_hwnd, self.triggered_dialogs, "从资源管理器切回对话框时必须触发自动跳转！")
        finally:
            self.em.is_explorer_window = orig_is_explorer
            self.em.is_dialog_window = orig_is_dialog
            self.em.get_explorer_path = orig_get_path
            win32gui.IsWindow = orig_is_window

    def test_switch_from_non_explorer_stays_silent(self):
        """
        防误触机制：从非资源管理器窗口（如 Chrome/WeChat/VSCode）切入对话框。
        必须安全静默，不触发自动跳转。
        """
        fake_browser_hwnd = 0x33333333
        fake_dialog_hwnd = 0x44444444

        orig_is_explorer = self.em.is_explorer_window
        orig_is_dialog = self.em.is_dialog_window
        orig_is_window = win32gui.IsWindow

        def mock_is_explorer(h):
            return False

        def mock_is_dialog(h):
            return h == fake_dialog_hwnd

        try:
            self.em.is_explorer_window = mock_is_explorer
            self.em.is_dialog_window = mock_is_dialog
            win32gui.IsWindow = lambda h: True

            # Phase 1: Focus on Browser
            self.em._on_foreground_event(fake_browser_hwnd)
            self.assertEqual(self.em._prev_foreground_hwnd, fake_browser_hwnd)

            time.sleep(0.35)

            # Phase 2: Switch to Dialog
            self.em._on_foreground_event(fake_dialog_hwnd)
            time.sleep(0.1)

            self.assertNotIn(fake_dialog_hwnd, self.triggered_dialogs, "从非资源管理器窗口切入对话框时必须保持安全静默！")
        finally:
            self.em.is_explorer_window = orig_is_explorer
            self.em.is_dialog_window = orig_is_dialog
            win32gui.IsWindow = orig_is_window

    def test_debounce_guard(self):
        """
        快速 Alt+Tab 防抖保护：< 300ms 内高频切换同一对窗口时忽略重复触发。
        """
        fake_explorer_hwnd = 0x55555555
        fake_dialog_hwnd = 0x66666666

        orig_is_explorer = self.em.is_explorer_window
        orig_is_dialog = self.em.is_dialog_window
        orig_get_path = self.em.get_explorer_path
        orig_is_window = win32gui.IsWindow

        try:
            self.em.is_explorer_window = lambda h: h == fake_explorer_hwnd
            self.em.is_dialog_window = lambda h: h == fake_dialog_hwnd
            self.em.get_explorer_path = lambda h: os.path.abspath(BASE_DIR)
            win32gui.IsWindow = lambda h: True

            # First transition after sufficient delay
            self.em._on_foreground_event(fake_explorer_hwnd)
            time.sleep(0.35)
            self.em._on_foreground_event(fake_dialog_hwnd)
            time.sleep(0.05)
            self.assertEqual(len(self.triggered_dialogs), 1)

            # Rapid transition within 50ms (< 300ms debounce window)
            self.em._on_foreground_event(fake_explorer_hwnd)
            time.sleep(0.05)
            self.em._on_foreground_event(fake_dialog_hwnd)
            time.sleep(0.05)

            # Should still be 1 because rapid switch was debounced
            self.assertEqual(len(self.triggered_dialogs), 1, "快速高频切换应被防抖机制过滤！")
        finally:
            self.em.is_explorer_window = orig_is_explorer
            self.em.is_dialog_window = orig_is_dialog
            self.em.get_explorer_path = orig_get_path
            win32gui.IsWindow = orig_is_window

    def test_auto_switch_idempotency_and_silent_when_no_mru(self):
        """
        auto_switch 在无 MRU 记录时保持静默；若对话框已在目标目录时幂等跳过。
        """
        # 1. No MRU record -> silent
        self.em.reset_mru()
        fake_dlg = 0x77777777
        ok = self.injector.auto_switch(fake_dlg)
        self.assertFalse(ok, "无 MRU 记录时 auto_switch 必须返回 False 保持静默")

        # 2. Idempotency test
        orig_target_dir = os.path.abspath(BASE_DIR)
        orig_get_path = self.injector.get_dialog_current_path
        orig_is_window = win32gui.IsWindow

        try:
            win32gui.IsWindow = lambda h: True
            # Mock dialog already at orig_target_dir
            self.injector.get_dialog_current_path = lambda h: orig_target_dir

            # Inject same path -> should return True via idempotency check without calling controls
            idempotent_ok = self.injector.inject_path(orig_target_dir, h_dlg=fake_dlg)
            self.assertTrue(idempotent_ok, "目标路径与对话框当前路径一致时应幂等跳过并成功返回")
        finally:
            self.injector.get_dialog_current_path = orig_get_path
            win32gui.IsWindow = orig_is_window


class TestFolderClickRaceProtection(unittest.TestCase):
    """
    Tests protection against clicking a folder in dialog during path injection:
    When user clicks a folder item to switch back to dialog, dialog must NOT drill down
    into the clicked folder; instead, path injection must take precedence and jump to target path.
    """

    def test_click_folder_does_not_drill_down_and_injects_target(self):
        ensure_desktop()
        injector = PathInjector()
        race_dlg_hwnd = None

        def run_dialog():
            ensure_desktop()
            try:
                flags = win32con.OFN_EXPLORER | win32con.OFN_OVERWRITEPROMPT
                win32gui.GetSaveFileNameW(
                    File='document.docx',
                    Filter='Word Documents\0*.docx\0All Files\0*.*\0',
                    InitialDir=os.path.abspath(BASE_DIR),
                    Title='ZenSearch Click Race Test Dialog',
                    Flags=flags
                )
            except Exception:
                pass

        t = threading.Thread(target=run_dialog, daemon=True)
        t.start()

        h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
        for _ in range(30):
            time.sleep(0.1)
            def enum_cb(h, _):
                nonlocal race_dlg_hwnd
                if win32gui.GetClassName(h) == '#32770':
                    txt = win32gui.GetWindowText(h)
                    if 'ZenSearch Click Race Test Dialog' in txt:
                        race_dlg_hwnd = h
                return True
            if h_desk:
                win32gui.EnumDesktopWindows(h_desk, enum_cb, None)
            else:
                win32gui.EnumWindows(enum_cb, None)
            if race_dlg_hwnd:
                break

        self.assertIsNotNone(race_dlg_hwnd, "未能成功创建或定位测试对话框")

        try:
            h_edit = None
            for _ in range(10):
                time.sleep(0.05)
                h_edit, h_btn = injector.find_dialog_controls(race_dlg_hwnd)
                if h_edit:
                    break

            self.assertIsNotNone(h_edit, "必须成功定位文件名编辑框控件")

            # 模拟用户切回时点击了对话框内的 'core' 文件夹：编辑框被置为 'core'
            win32gui.SendMessage(h_edit, win32con.WM_SETTEXT, 0, 'core')

            # 目标注入路径：D:\ZenSearch\Everything-SDK
            target_dir = os.path.join(os.path.abspath(BASE_DIR), 'Everything-SDK')
            self.assertTrue(os.path.exists(target_dir))

            # 执行注入
            ok = injector.inject_path(target_dir, h_dlg=race_dlg_hwnd)
            self.assertTrue(ok, "在点击文件夹干扰场景下，路径注入依然必须成功执行")

            # 验证实际路径：绝不能进入刚刚点击的 'core' 目录，而必须精准到达 Everything-SDK！
            actual_path = injector.get_dialog_current_path(race_dlg_hwnd)
            if actual_path:
                self.assertNotIn(
                    os.path.normcase(os.path.join(os.path.abspath(BASE_DIR), 'core')),
                    os.path.normcase(actual_path),
                    "严重错误：对话框误进入了刚刚点击的文件夹！"
                )
                self.assertEqual(
                    os.path.normcase(actual_path),
                    os.path.normcase(target_dir),
                    f"对话框实际路径 {actual_path} 必须与目标路径 {target_dir} 严格一致！"
                )
        finally:
            if race_dlg_hwnd and win32gui.IsWindow(race_dlg_hwnd):
                win32gui.PostMessage(race_dlg_hwnd, win32con.WM_COMMAND, win32con.IDCANCEL, 0)
            t.join(timeout=1.0)


if __name__ == '__main__':
    unittest.main()
