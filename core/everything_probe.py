"""
ZenSearch - Everything Environment Probe & Lifecycle Linkage Manager
Implements a 4-tier detection pipeline to probe Everything installation,
auto-launches Everything in background (-startup) with IPC polling,
and offers interactive prompt / graceful degradation when Everything is missing.
"""

import os
import sys
import time
import shutil
import winreg
import subprocess
from dataclasses import dataclass
from typing import Optional, List, Tuple

import win32gui
import win32process
from core.logger import logger


@dataclass
class ProbeResult:
    is_running: bool
    installed: bool
    exe_path: Optional[str] = None
    source: str = "none"
    details: str = ""


class EverythingProbe:
    """
    Multi-tier detection and lifecycle management for Everything search engine.
    """

    OFFICIAL_DOWNLOAD_URL = "https://www.voidtools.com/zh-cn/"

    @classmethod
    def probe_environment(cls) -> ProbeResult:
        """
        Executes the 4-tier detection pipeline:
        1. Level 1: In-memory running state & notification window sniffing
        2. Level 2: Windows System Registry deep scanning (voidtools, Uninstall, App Paths)
        3. Level 3: System PATH and environment probing
        4. Level 4: Standard physical installation directories probing
        """
        # --- Level 1: Memory & Window Running State ---
        running_hwnd, running_title = cls._probe_running_window()
        if running_hwnd:
            return ProbeResult(
                is_running=True,
                installed=True,
                exe_path=cls._get_exe_from_hwnd(running_hwnd),
                source="level1_window",
                details=f"正在运行 (HWND: {running_hwnd}, Title: '{running_title}')"
            )

        # Check running process by name even if window is hidden
        running_pid_path = cls._probe_running_process()
        if running_pid_path:
            return ProbeResult(
                is_running=True,
                installed=True,
                exe_path=running_pid_path,
                source="level1_process",
                details="已在后台进程运行"
            )

        # --- Level 2: Registry Scanning ---
        reg_path = cls._probe_registry()
        if reg_path and os.path.exists(reg_path):
            return ProbeResult(
                is_running=False,
                installed=True,
                exe_path=reg_path,
                source="level2_registry",
                details=f"注册表发现安装路径: {reg_path}"
            )

        # --- Level 3: PATH Probing ---
        path_exe = cls._probe_path()
        if path_exe and os.path.exists(path_exe):
            return ProbeResult(
                is_running=False,
                installed=True,
                exe_path=path_exe,
                source="level3_path",
                details=f"环境变量 PATH 发现可执行程序: {path_exe}"
            )

        # --- Level 4: Standard Physical Directories ---
        standard_exe = cls._probe_physical_paths()
        if standard_exe and os.path.exists(standard_exe):
            return ProbeResult(
                is_running=False,
                installed=True,
                exe_path=standard_exe,
                source="level4_physical",
                details=f"标准物理目录发现可执行程序: {standard_exe}"
            )

        # None found -> not installed
        return ProbeResult(
            is_running=False,
            installed=False,
            exe_path=None,
            source="not_found",
            details="未检测到任何 Everything 安装迹象"
        )

    @classmethod
    def _probe_running_window(cls) -> Tuple[Optional[int], str]:
        # 1. Standard taskbar notification window
        hwnd = win32gui.FindWindow('EVERYTHING_TASKBAR_NOTIFICATION', None)
        if hwnd and win32gui.IsWindow(hwnd):
            return hwnd, win32gui.GetWindowText(hwnd)

        # 2. Any window with EVERYTHING class
        found = []
        def enum_cb(h, _):
            try:
                c = win32gui.GetClassName(h)
                if 'EVERYTHING' in c.upper():
                    found.append((h, win32gui.GetWindowText(h)))
            except Exception:
                pass

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            pass

        if found:
            return found[0][0], found[0][1]

        return None, ""

    @classmethod
    def _get_exe_from_hwnd(cls, hwnd: int) -> Optional[str]:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid:
                import psutil
                proc = psutil.Process(pid)
                return proc.exe()
        except Exception:
            pass
        return None

    @classmethod
    def _probe_running_process(cls) -> Optional[str]:
        try:
            import psutil
            for p in psutil.process_iter(['pid', 'name', 'exe']):
                if p.info['name'] and 'everything' in p.info['name'].lower():
                    exe = p.info.get('exe')
                    if exe and os.path.exists(exe):
                        return exe
        except Exception:
            pass
        return None

    @classmethod
    def _probe_registry(cls) -> Optional[str]:
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
            (winreg.HKEY_CURRENT_USER, "HKCU")
        ]

        subkeys = [
            # voidtools official key
            (r"SOFTWARE\voidtools\Everything", ["InstallLocation", "InstallPath"]),
            # System uninstall entries (64-bit and WOW6432Node)
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Everything", ["DisplayIcon", "InstallLocation"]),
            (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Everything", ["DisplayIcon", "InstallLocation"]),
            # App Paths
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Everything.exe", ["", "Path"]),
        ]

        for hkey, _ in roots:
            for subkey_name, value_names in subkeys:
                try:
                    with winreg.OpenKey(hkey, subkey_name, 0, winreg.KEY_READ) as key:
                        for vname in value_names:
                            try:
                                val, _ = winreg.QueryValueEx(key, vname)
                                if val:
                                    clean_val = str(val).strip('"').split(',')[0].strip()
                                    if clean_val.lower().endswith(".exe") and os.path.isfile(clean_val):
                                        return clean_val
                                    elif os.path.isdir(clean_val):
                                        candidate = os.path.join(clean_val, "Everything.exe")
                                        if os.path.isfile(candidate):
                                            return candidate
                            except FileNotFoundError:
                                continue
                except Exception:
                    continue
        return None

    @classmethod
    def _probe_path(cls) -> Optional[str]:
        found = shutil.which("Everything.exe") or shutil.which("everything.exe") or shutil.which("Everything")
        return os.path.abspath(found) if found else None

    @classmethod
    def _probe_physical_paths(cls) -> Optional[str]:
        candidates = [
            r"D:\Install_path\Everything\Everything.exe",
            os.path.expandvars(r"%ProgramFiles%\Everything\Everything.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Everything\Everything.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Everything\Everything.exe"),
            os.path.expandvars(r"%APPDATA%\Everything\Everything.exe"),
            r"C:\Everything\Everything.exe",
            r"D:\Everything\Everything.exe",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return os.path.abspath(c)
        return None

    @classmethod
    def launch_silent(cls, exe_path: str) -> bool:
        """
        Launches Everything with -startup flag to keep it headless / in tray only.
        """
        if not exe_path or not os.path.isfile(exe_path):
            logger.error(f"[EverythingProbe] 指定的 Everything 路径不存在: {exe_path}")
            return False

        logger.info(f"[EverythingProbe] 正在以后台静默方式拉起 Everything: {exe_path} -startup")
        try:
            si = win32process.STARTUPINFO()
            si.lpDesktop = 'Default'
            win32process.CreateProcess(
                exe_path,
                f'"{exe_path}" -startup',
                None,
                None,
                False,
                0,
                None,
                os.path.dirname(exe_path),
                si
            )
            return True
        except Exception as e:
            logger.debug(f"[EverythingProbe] win32process.CreateProcess 启动失败 ({e})，尝试 subprocess.Popen...")
            try:
                subprocess.Popen([exe_path, "-startup"], cwd=os.path.dirname(exe_path))
                return True
            except Exception as ex:
                logger.error(f"[EverythingProbe] subprocess 启动 Everything 失败: {ex}")
                return False

    @classmethod
    def wait_for_ipc_ready(cls, timeout_sec: float = 3.0) -> bool:
        """
        Polls for Everything notification window every 150ms up to timeout_sec.
        """
        start = time.time()
        while time.time() - start < timeout_sec:
            hwnd = win32gui.FindWindow('EVERYTHING_TASKBAR_NOTIFICATION', None)
            if hwnd and win32gui.IsWindow(hwnd):
                logger.info(f"[EverythingProbe] Everything IPC 服务就绪 (HWND: {hwnd})，耗时: {(time.time()-start)*1000:.1f}ms")
                return True
            time.sleep(0.15)
        logger.warning(f"[EverythingProbe] 等待 Everything IPC 就绪超时 ({timeout_sec}s)")
        return False

    @classmethod
    def ensure_everything_running(cls) -> Tuple[bool, ProbeResult]:
        """
        Orchestrates full probe and lifecycle sync:
        - If already running -> returns (True, result)
        - If installed but stopped -> auto launches with -startup and polls for ready -> returns (is_ready, result)
        - If not installed -> returns (False, result)
        """
        probe = cls.probe_environment()
        if probe.is_running:
            return True, probe

        if probe.installed and probe.exe_path:
            logger.info(f"[EverythingProbe] 检测到 Everything 已安装 ({probe.exe_path}) 但未运行，自动后台静默拉起...")
            launched = cls.launch_silent(probe.exe_path)
            if launched:
                ready = cls.wait_for_ipc_ready(timeout_sec=3.0)
                probe.is_running = ready
                return ready, probe
            else:
                return False, probe

        logger.warning("[EverythingProbe] 系统未检测到 Everything 安装")
        return False, probe
