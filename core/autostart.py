"""
ZenSearch - Auto Startup Manager
Manages Windows User Run Registry (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run)
Supports headless/silent execution via pythonw.exe and self-healing for invalid or moved paths.
"""

import os
import sys
import winreg
from typing import Optional, Tuple
from core.logger import logger


REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ZenSearch"


class AutoStartManager:
    """
    Manages user-level auto startup on boot for ZenSearch.
    Operates on HKCU to ensure no UAC elevation prompt is needed.
    """

    @staticmethod
    def get_target_command(silent: bool = True) -> str:
        """
        Builds the robust startup command string with quoted paths.
        Uses pythonw.exe instead of python.exe to suppress console window.
        """
        main_py = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py"))
        
        # Check if running from compiled frozen exe or python script
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
            cmd = f'"{exe_path}"'
            if silent:
                cmd += ' --silent'
            return cmd
        
        # In python environment, locate pythonw.exe in same directory as python.exe
        python_exe = sys.executable
        py_dir = os.path.dirname(python_exe)
        pythonw_exe = os.path.join(py_dir, "pythonw.exe")
        
        if os.path.exists(pythonw_exe):
            launcher_exe = pythonw_exe
        else:
            launcher_exe = python_exe

        cmd = f'"{launcher_exe}" "{main_py}"'
        if silent:
            cmd += ' --silent'
        return cmd

    @classmethod
    def is_autostart_enabled(cls) -> bool:
        """
        Checks whether ZenSearch is currently registered in HKCU Run key.
        """
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, APP_NAME)
                return bool(val and val.strip())
        except FileNotFoundError:
            return False
        except Exception as e:
            logger.debug(f"[AutoStart] 读取自启动注册表异常: {e}")
            return False

    @classmethod
    def get_registered_command(cls) -> Optional[str]:
        """
        Returns the command currently registered in the registry, or None.
        """
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, APP_NAME)
                return str(val)
        except Exception:
            return None

    @classmethod
    def set_autostart(cls, enabled: bool, silent: bool = True) -> bool:
        """
        Enables or disables auto startup in HKCU.
        Returns True if successful, False otherwise.
        """
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    cmd = cls.get_target_command(silent=silent)
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                    logger.info(f"[AutoStart] 已启用开机自启动: {cmd}")
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                        logger.info("[AutoStart] 已关闭开机自启动")
                    except FileNotFoundError:
                        pass
            return True
        except Exception as e:
            logger.error(f"[AutoStart] 配置开机自启动失败: {e}")
            return False

    @classmethod
    def self_heal_if_needed(cls) -> Tuple[bool, str]:
        """
        Validates registered startup command. If enabled but target path has changed
        or points to an invalid/moved file, automatically updates registry to current path.
        Returns (was_healed, message).
        """
        if not cls.is_autostart_enabled():
            return False, "未开启自启动，无需自愈"

        current_reg_cmd = cls.get_registered_command()
        expected_cmd = cls.get_target_command(silent=True)

        if not current_reg_cmd:
            return False, "注册表键值为空"

        # Check if the script/binary in the registered command still exists
        needs_heal = False
        reason = ""

        # Extract file paths inside quotes
        import re
        tokens = re.findall(r'"([^"]+)"', current_reg_cmd)
        for path_token in tokens:
            if not os.path.exists(path_token):
                needs_heal = True
                reason = f"引用的路径不存在或已迁移: {path_token}"
                break

        if not needs_heal:
            # Check if expected command differs significantly from registered command
            main_py = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py"))
            if main_py.lower() not in current_reg_cmd.lower():
                needs_heal = True
                reason = f"程序工作路径已迁移 (当前: {main_py})"

        if needs_heal:
            logger.warning(f"[AutoStart] 检测到自启动配置异常 ({reason})，正在自愈更新...")
            success = cls.set_autostart(True, silent=True)
            if success:
                logger.info(f"[AutoStart] 自愈完成，更新命令为: {expected_cmd}")
                return True, f"已自愈更新: {reason}"
            else:
                return False, f"自愈失败: {reason}"

        return False, "自启动配置正常"
