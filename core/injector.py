"""
ZenSearch - Path Injection (Quick Switch) Engine
Reference: 路径注入.md, ZenSearch技术方案.md

Injects the target directory path into Windows Common File Dialogs (#32770)
to achieve instant, millisecond-level directory jumping while adhering to
the Strict MRU Policy (严格 MRU 定向原则).
"""

import os
import time
import win32gui
import win32con
import win32process
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple, List
from core.explorer import ExplorerManager, ensure_desktop
from core.logger import logger

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
BM_CLICK = 0x00F5


def _wait_mouse_button_up(timeout: float = 0.35) -> None:
    """
    Waits until the left mouse button is physically released, plus a small grace period
    to allow the dialog to finish processing any pending click/selection change messages.
    Prevents race condition where mouse click selects a folder and overwrites injection.
    """
    start_time = time.time()
    clicked = False
    while (user32.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000) and (time.time() - start_time < timeout):
        clicked = True
        time.sleep(0.01)
    if clicked or (time.time() - start_time > 0.01):
        # Allow Windows message queue to dispatch WM_LBUTTONUP and list selection notifications
        time.sleep(0.05)


def _set_focus_to_control(h_dlg: int, h_ctrl: int) -> None:
    """Sets dialog keyboard focus to the specified control safely."""
    try:
        # Standard Win32 dialog focus message
        win32gui.SendMessage(h_dlg, win32con.WM_NEXTDLGCTL, h_ctrl, 1)

        # AttachThreadInput for cross-thread SetFocus
        tid_dlg, _ = win32process.GetWindowThreadProcessId(h_dlg)
        tid_cur = kernel32.GetCurrentThreadId()
        attached = False
        if tid_dlg and tid_dlg != tid_cur:
            attached = bool(user32.AttachThreadInput(tid_cur, tid_dlg, True))
        try:
            user32.SetFocus(h_ctrl)
        finally:
            if attached:
                user32.AttachThreadInput(tid_cur, tid_dlg, False)
    except Exception:
        pass


class PathInjector:
    def __init__(self):
        self.explorer_mgr = ExplorerManager.get_instance()

    def find_dialog_controls(self, h_dlg: int) -> Tuple[Optional[int], Optional[int]]:
        """
        Locates (Edit control, Open/Save Button) inside a #32770 Common File Dialog.
        Prioritizes the standard filename edit box (Control ID 0x47c / 1148, 1001) and IDOK button (ID 1).
        """
        ensure_desktop()
        edit_candidates: List[Tuple[int, int]] = []  # (priority, hwnd)
        btn_hwnd: Optional[int] = None

        def enum_child_proc(hwnd, _):
            nonlocal btn_hwnd
            try:
                cls_name = win32gui.GetClassName(hwnd)
                ctrl_id = win32gui.GetWindowLong(hwnd, win32con.GWL_ID)
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)

                is_truly_visible = bool(win32gui.IsWindowVisible(hwnd))
                has_visible_style = bool(style & win32con.WS_VISIBLE)
                is_enabled = not bool(style & win32con.WS_DISABLED)

                if cls_name == 'Edit' and (is_truly_visible or has_visible_style) and is_enabled:
                    parent = win32gui.GetParent(hwnd)
                    p_cls = win32gui.GetClassName(parent) if parent else ''

                    # Exclude search boxes and address bar edits
                    if 'Search' in p_cls or 'Breadcrumb' in p_cls or 'Address' in p_cls or ctrl_id == 41477:
                        pass
                    else:
                        base_priority = 10 if is_truly_visible else 0
                        if ctrl_id in (0x47c, 1001):
                            # Standard filename edit: 0x47c (1148) in OFN/classic, 1001 in modern IFileDialog
                            edit_candidates.append((base_priority + 10, hwnd))
                        elif ctrl_id == 0x480:
                            # Classic edt1 (1152)
                            edit_candidates.append((base_priority + 8, hwnd))
                        else:
                            edit_candidates.append((base_priority + 5, hwnd))

                elif cls_name == 'Button' and (is_truly_visible or has_visible_style) and is_enabled:
                    # Open/Save button typically has ID 1 (IDOK)
                    if ctrl_id == 1:
                        btn_hwnd = hwnd
                    elif btn_hwnd is None:
                        txt = win32gui.GetWindowText(hwnd)
                        if any(k in txt for k in ('打开', '保存', 'Open', 'Save', '选择文件夹', 'Select')):
                            btn_hwnd = hwnd
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(h_dlg, enum_child_proc, None)
        except Exception:
            pass

        # Sort edit candidates by priority descending
        edit_candidates.sort(key=lambda x: x[0], reverse=True)
        h_edit = edit_candidates[0][1] if edit_candidates else None

        return h_edit, btn_hwnd

    def find_dialog_edit_control(self, h_dlg: int) -> Optional[int]:
        """Backward compatibility helper."""
        h_edit, _ = self.find_dialog_controls(h_dlg)
        return h_edit

    def get_dialog_current_path(self, h_dlg: int) -> Optional[str]:
        """
        Inspects the current path of the Common File Dialog via its Breadcrumb/Address toolbar.
        Returns the resolved absolute path, or None if not accessible.
        """
        ensure_desktop()
        if not h_dlg or not win32gui.IsWindow(h_dlg):
            return None

        detected_path = None

        def enum_c(h, _):
            nonlocal detected_path
            try:
                if win32gui.GetClassName(h) == 'ToolbarWindow32':
                    txt = win32gui.GetWindowText(h)
                    if '地址:' in txt or 'Address:' in txt:
                        parts = txt.split(':', 1)
                        if len(parts) > 1:
                            val = parts[1].strip()
                            if val and os.path.exists(val):
                                detected_path = os.path.abspath(val)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumChildWindows(h_dlg, enum_c, None)
        except Exception:
            pass

        return detected_path

    def inject_path(self, target_path: str, h_dlg: Optional[int] = None, _retry_count: int = 0) -> bool:
        """
        Injects target_path into the file dialog (#32770) and triggers directory navigation.
        Preserves existing user-typed filename and verifies actual directory transition.
        Guards against click-selection races, folder-drilldown misdirection, and edit tampering.
        """
        ensure_desktop()
        if not target_path or not os.path.exists(target_path):
            logger.warning(f"[Injector] 目标路径不存在或无效: {target_path}")
            return False

        # If user activated the dialog via mouse click, wait for physical mouse release
        # and allow the dialog list view to finish processing the click and selection updates.
        _wait_mouse_button_up()

        # Normalize directory path
        norm_target = os.path.abspath(target_path)
        # Ensure trailing backslash so Common File Dialog unambiguously treats it as directory navigation
        target_path_with_slash = norm_target if norm_target.endswith('\\') else (norm_target + '\\')

        if h_dlg is None:
            h_dlg = self.explorer_mgr.find_active_dialog()

        if not h_dlg or not win32gui.IsWindow(h_dlg):
            logger.warning("[Injector] 未检测到活动的文件对话框 (#32770)")
            return False

        # Idempotency check: if dialog is already at target path, skip injection to avoid flicker
        current_dlg_path = self.get_dialog_current_path(h_dlg)
        if current_dlg_path and os.path.normcase(current_dlg_path) == os.path.normcase(norm_target):
            logger.info(f"[Injector] 对话框 [HWND:{h_dlg}] 当前路径已是目标路径 ({norm_target})，幂等跳过注入")
            return True

        h_edit, h_btn = self.find_dialog_controls(h_dlg)
        if not h_edit:
            for _ in range(6):
                time.sleep(0.05)
                h_edit, h_btn = self.find_dialog_controls(h_dlg)
                if h_edit:
                    break

        if not h_edit:
            logger.warning(f"[Injector] 对话框 [HWND:{h_dlg}] 中未找到可注入的文件名编辑框控件")
            return False

        try:
            # 1. Bring dialog to foreground if needed
            try:
                win32gui.SetForegroundWindow(h_dlg)
            except Exception:
                pass

            # 2. Check and preserve original filename (if user had typed e.g. "MyDoc.docx")
            buf = ctypes.create_unicode_buffer(512)
            user32.SendMessageW(h_edit, win32con.WM_GETTEXT, 512, buf)
            orig_filename = buf.value.strip()

            # Guard: If orig_filename is actually an existing folder under current dialog directory,
            # it was placed into h_edit by the user's mouse click / selection, NOT user-typed filename!
            if current_dlg_path and orig_filename:
                candidate_subfolder = os.path.join(current_dlg_path, orig_filename)
                if os.path.isdir(candidate_subfolder):
                    orig_filename = ""

            # 3. Bring keyboard focus to h_edit and inject target directory path
            _set_focus_to_control(h_dlg, h_edit)
            win32gui.SendMessage(h_edit, win32con.WM_SETTEXT, 0, target_path_with_slash)
            time.sleep(0.04)

            # Re-verify that h_edit wasn't overwritten by any delayed mouse click / selection message
            user32.SendMessageW(h_edit, win32con.WM_GETTEXT, 512, buf)
            if buf.value.strip() not in (target_path_with_slash.strip(), norm_target):
                _set_focus_to_control(h_dlg, h_edit)
                win32gui.SendMessage(h_edit, win32con.WM_SETTEXT, 0, target_path_with_slash)
                time.sleep(0.02)

            # 4. Trigger navigation:
            # Primary: Send VK_RETURN directly to h_edit (native directory navigation)
            win32gui.PostMessage(h_edit, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32gui.PostMessage(h_edit, win32con.WM_KEYUP, win32con.VK_RETURN, 0)

            # 5. Verify navigation and handle fallback
            navigated = False
            for _ in range(8):  # Check up to ~400ms
                time.sleep(0.05)
                current_p = self.get_dialog_current_path(h_dlg)
                if current_p and os.path.normcase(current_p) == os.path.normcase(norm_target):
                    navigated = True
                    break

            # Fallback if VK_RETURN didn't trigger navigation within 400ms:
            if not navigated:
                # Ensure h_edit still contains target path before fallback click
                user32.SendMessageW(h_edit, win32con.WM_GETTEXT, 512, buf)
                if buf.value.strip() not in (target_path_with_slash.strip(), norm_target):
                    _set_focus_to_control(h_dlg, h_edit)
                    win32gui.SendMessage(h_edit, win32con.WM_SETTEXT, 0, target_path_with_slash)
                    time.sleep(0.03)

                if h_btn and win32gui.IsWindow(h_btn):
                    win32gui.SendMessage(h_btn, BM_CLICK, 0, 0)
                else:
                    win32gui.PostMessage(h_dlg, win32con.WM_COMMAND, 1, 0)

                for _ in range(8):  # Check up to another ~400ms
                    time.sleep(0.05)
                    current_p = self.get_dialog_current_path(h_dlg)
                    if current_p and os.path.normcase(current_p) == os.path.normcase(norm_target):
                        navigated = True
                        break

            # 6. Check edit box state post-navigation:
            # If the edit control still contains target_path_with_slash or norm_target, clean it up or restore original filename
            user32.SendMessageW(h_edit, win32con.WM_GETTEXT, 512, buf)
            curr_edit_text = buf.value.strip()
            if curr_edit_text in (target_path_with_slash, norm_target):
                if orig_filename and (orig_filename != norm_target) and ('\\' not in orig_filename) and ('/' not in orig_filename):
                    win32gui.SendMessage(h_edit, win32con.WM_SETTEXT, 0, orig_filename)
                else:
                    win32gui.SendMessage(h_edit, win32con.WM_SETTEXT, 0, '')

            # Keep keyboard focus on edit control for seamless user experience
            _set_focus_to_control(h_dlg, h_edit)

            # 7. Final verification check & Self-healing retry:
            final_p = self.get_dialog_current_path(h_dlg)
            if final_p and os.path.normcase(final_p) != os.path.normcase(norm_target):
                if _retry_count < 1:
                    logger.info(
                        f"[Injector] 对话框 [HWND:{h_dlg}] 实际跳转路径 ({final_p}) 与期望目标路径 ({norm_target}) 发生偏离，启动自愈补偿重试..."
                    )
                    time.sleep(0.05)
                    return self.inject_path(target_path, h_dlg=h_dlg, _retry_count=_retry_count + 1)
                else:
                    logger.warning(
                        f"[Injector] 对话框 [HWND:{h_dlg}] 真实跳转路径 ({final_p}) 与期望目标路径 ({norm_target}) 不一致，注入未生效"
                    )
                    return False

            logger.info(f"[Injector] 成功向对话框 [HWND:{h_dlg}] 注入路径: {norm_target}")
            return True
        except Exception as e:
            logger.error(f"[Injector] 注入路径时发生异常: {e}")
            return False

    def auto_switch(self, h_dlg: int) -> bool:
        """
        Executed on switch-back focus event (from Explorer to Dialog).
        Performs Strict MRU retrieval, idempotency verification and path injection.
        """
        logger.info(f"[Injector] 触发切回自动跳转流程 -> 对话框 [HWND:{h_dlg}]")
        target_path = self.explorer_mgr.get_last_active_explorer_path()
        if not target_path:
            logger.info("[Injector] [严格MRU定向] 未获取到上一个浏览的资源管理器路径，切回自动跳转安全静默")
            return False
        return self.inject_path(target_path, h_dlg=h_dlg)

    def quick_switch(self, h_dlg: Optional[int] = None) -> bool:
        """
        Performs Quick Switch (e.g. manual Ctrl+G): grabs the last active Explorer path under
        Strict MRU Policy and injects it into current active file dialog.
        """
        target_path = self.explorer_mgr.get_last_active_explorer_path()
        if not target_path:
            logger.info("[Injector] [严格MRU定向] 未获取到上一个浏览的资源管理器路径，拒绝注入并安全静默")
            return False
        return self.inject_path(target_path, h_dlg=h_dlg)
