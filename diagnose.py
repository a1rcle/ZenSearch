"""
ZenSearch - 系统运行环境与核心功能全面诊断工具 (Diagnostic & Debug Tool)
用于一键排查 Everything IPC、键盘钩子、资源管理器 COM 互操作及路径注入的各类故障。

运行方式:
    python diagnose.py          # 执行全面自动诊断并生成报告
    python diagnose.py --watch  # 开启实时键盘钩子与窗口焦点侦听器
"""

import os
import sys
import time
import struct
import argparse
import ctypes
from ctypes import wintypes
from typing import List

# 将工程目录添加至 sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import win32gui
import win32con
import win32process
import win32service
import win32api
import win32security

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]

user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]

user32.GetUserObjectInformationW.restype = wintypes.BOOL
user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

user32.CallNextHookEx.restype = ctypes.c_longlong
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]

user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD
]

user32.UnhookWinEvent.restype = wintypes.BOOL
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

user32.GetMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

user32.TranslateMessage.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

user32.DispatchMessageW.restype = ctypes.c_longlong
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GetLastError.argtypes = []

kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def print_header(title: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def print_item(status: str, name: str, details: str = ""):
    badge = {
        "PASS": "\033[92m[✓ PASS]\033[0m",
        "FAIL": "\033[91m[✗ FAIL]\033[0m",
        "WARN": "\033[93m[! WARN]\033[0m",
        "INFO": "\033[94m[i INFO]\033[0m"
    }.get(status, f"[{status}]")
    print(f"{badge:<12} {name:<28} {details}")

# ==========================================
# 1. 运行环境与权限诊断
# ==========================================
def diagnose_environment():
    print_header("1. 基础环境与权限诊断")

    # 尝试切换至用户真实交互桌面 Default
    try:
        h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
        if h_desk:
            user32.SetThreadDesktop(h_desk)
    except Exception:
        pass

    # Python 版本与位数
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    bitness = struct.calcsize('P') * 8
    print_item("PASS", "Python 运行时", f"{py_ver} ({bitness}-bit)")

    # 依赖库检查
    for mod in ["PyQt5", "win32gui", "win32com", "psutil"]:
        try:
            __import__(mod)
            print_item("PASS", f"依赖库: {mod}", "已正确安装")
        except ImportError:
            print_item("FAIL", f"依赖库: {mod}", "未安装，请执行 pip install " + mod)

    # 进程权限等级 (Integrity / Elevation)
    try:
        h_proc = win32process.GetCurrentProcess()
        h_tok = win32security.OpenProcessToken(h_proc, win32security.TOKEN_QUERY)
        elevated = bool(win32security.GetTokenInformation(h_tok, win32security.TokenElevation))
        if elevated:
            print_item("INFO", "进程权限等级", "管理员权限 (Elevated: 1)")
        else:
            print_item("INFO", "进程权限等级", "普通标准用户权限 (Elevated: 0)")
    except Exception as e:
        print_item("WARN", "进程权限探测", f"无法探测权限状态: {e}")

    # Desktop 桌面工作站
    try:
        h_desk = user32.GetThreadDesktop(win32api.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(256)
        user32.GetUserObjectInformationW(h_desk, 2, buf, 256, None)
        desk_name = buf.value
        if desk_name.lower() == "default":
            print_item("PASS", "WindowStation/Desktop", f"交互式桌面: {desk_name}")
        else:
            print_item("WARN", "WindowStation/Desktop", f"非默认桌面 ({desk_name})，已尝试挂载 Default")
    except Exception as e:
        print_item("WARN", "桌面环境检测", str(e))

    # High DPI 与多显示器缩放自检
    try:
        from ui.scale_manager import init_high_dpi_context, UiScaleManager
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import Qt

        init_high_dpi_context()
        app = QApplication.instance()
        created_app = False
        if not app:
            app = QApplication(sys.argv)
            created_app = True

        scale_mgr = UiScaleManager.get_instance()
        screens = app.screens()
        print_item("PASS", "多显示器检测", f"系统共检测到 {len(screens)} 个活动显示器")
        for i, scr in enumerate(screens):
            geom = scr.geometry()
            avail = scr.availableGeometry()
            dpr = scr.devicePixelRatio()
            scale_mgr.update_for_screen(scr)
            w_launcher = scale_mgr.compute_launcher_width(scr)
            max_items = scale_mgr.compute_launcher_max_visible_items(scr)
            print(f"         ├─ [Screen {i}] 分辨率: {geom.width()}x{geom.height()} (有效工作区: {avail.width()}x{avail.height()}) | DPR: {dpr:.2f} | 缩放比率 S_res: {scale_mgr.scale_res:.2f}")
            print(f"         │     └─ 推荐主浮窗宽: {w_launcher}px (DIP) | 最大可见项数: {max_items}")

        target_scr = scale_mgr.get_target_screen()
        target_name = target_scr.name() if target_scr else "Unknown"
        print_item("PASS", "焦点屏幕智能嗅探", f"当前视线焦点定位在屏幕: {target_name}")

        if created_app:
            # Keep app instance if needed or let Python clean it up
            pass
    except Exception as e:
        print_item("WARN", "高 DPI / 屏幕适配探测", str(e))

# ==========================================
# 2. Everything IPC 连通性深度诊断
# ==========================================
def diagnose_everything():
    print_header("2. Everything 检索引擎与多源探针诊断")

    # 1. 四级探针全量扫描
    try:
        from core.everything_probe import EverythingProbe
        probe = EverythingProbe.probe_environment()
        status_str = "PASS" if probe.installed else "FAIL"
        print_item(status_str, "Everything 多源探针", f"命中来源: [{probe.source}] | 详情: {probe.details}")
        if probe.exe_path:
            print(f"         ├─ 可执行文件物理路径: {probe.exe_path}")
            print(f"         ├─ 是否正在运行态: {'是' if probe.is_running else '否'}")
    except Exception as e:
        print_item("WARN", "Everything 多源探针", f"探针探测异常: {e}")

    # 2. 查找进程
    found_pids = []
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name']):
            if 'everything' in p.info['name'].lower():
                found_pids.append((p.info['pid'], p.info['name']))
    except Exception:
        pass

    if found_pids:
        print_item("PASS", "Everything 进程", f"找到 {len(found_pids)} 个实例: {found_pids}")
    else:
        print_item("WARN", "Everything 进程", "未在进程列表中检测到 Everything.exe")

    # 3. 查找 IPC 窗口
    ipc_windows = []
    def enum_wnd(h, _):
        try:
            cls = win32gui.GetClassName(h)
            if 'EVERYTHING' in cls.upper():
                ipc_windows.append((h, cls))
        except Exception:
            pass

    try:
        h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
        if h_desk:
            win32gui.EnumDesktopWindows(h_desk, enum_wnd, None)
        else:
            win32gui.EnumWindows(enum_wnd, None)
    except Exception:
        win32gui.EnumWindows(enum_wnd, None)

    if ipc_windows:
        for hwnd, cls_name in ipc_windows:
            try:
                major = win32gui.SendMessage(hwnd, win32con.WM_USER, 0, 0)
                minor = win32gui.SendMessage(hwnd, win32con.WM_USER, 1, 0)
                build = win32gui.SendMessage(hwnd, win32con.WM_USER, 3, 0)
                ver_str = f"v{major}.{minor}.{build}"
                print_item("PASS", "Everything IPC 窗口", f"句柄 {hwnd} [{cls_name}] - 版本: {ver_str}")
            except Exception as e:
                print_item("WARN", "Everything IPC 窗口", f"句柄 {hwnd} 响应异常: {e}")
    else:
        print_item("FAIL", "Everything IPC 窗口", "未找到类名为 EVERYTHING_TASKBAR_NOTIFICATION 的窗口！")
        print("    [建议] 请确保系统已启动 Everything 且选项中已勾选运行在后台/托盘。")

    # 4. 尝试执行实际查询
    try:
        from core.everything_client import EverythingClient
        client = EverythingClient.get_instance()
        if client.is_available():
            t0 = time.time()
            results = client.search("ZenSearch", max_results=5)
            dt_ms = (time.time() - t0) * 1000
            print_item("PASS", "Everything 查询测试", f"耗时: {dt_ms:.1f}ms, 返回 {len(results)} 条结果")
            for i, r in enumerate(results[:3]):
                print(f"         └─ [{i+1}] {r.full_path}")
        else:
            print_item("FAIL", "EverythingClient 模块", "client.is_available() 返回 False")
    except Exception as e:
        print_item("FAIL", "EverythingClient 异常", str(e))

# ==========================================
# 3. 资源管理器 COM 互操作与路径嗅探
# ==========================================
def diagnose_explorer():
    print_header("3. Windows 资源管理器互操作诊断")
    try:
        from core.explorer import ExplorerManager
        em = ExplorerManager.get_instance()

        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("Shell.Application")
        windows = shell.Windows()
        count = windows.Count
        print_item("INFO", "资源管理器窗口统计", f"系统当前检测到 {count} 个 Shell 窗口")

        valid_folders = []
        for i in range(count):
            try:
                w = windows.Item(i)
                hwnd = getattr(w, 'HWND', 0)
                name = getattr(w, 'LocationName', '')
                url = getattr(w, 'LocationURL', '')
                path = em.get_explorer_path(hwnd) if hwnd else None
                valid_folders.append((hwnd, name, path or url))
                print(f"         ├─ [HWND: {hwnd}] 标题: '{name}' | 路径: {path or url}")
            except Exception as ex:
                print(f"         ├─ 窗口 {i} 抓取异常: {ex}")

        pythoncom.CoUninitialize()

        # 检查 MRU 跟踪状态
        if em._last_explorer_hwnd:
            print_item("PASS", "最新活动窗口记录", f"HWND: {em._last_explorer_hwnd} | 路径: {em._last_explorer_path}")
        else:
            print_item("INFO", "最新活动窗口记录", "暂无前序浏览窗口记录（未聚焦过 Explorer 窗口）")

        # 严格 MRU 原则测试
        # 测试 1: 当前是否有有效 MRU 路径
        last_path = em.get_last_active_explorer_path()
        if last_path:
            print_item("PASS", "严格MRU路径嗅探", f"成功抓取当前活动窗口路径: {last_path}")
        else:
            print_item("INFO", "严格MRU路径嗅探", "当前无活动浏览窗口，已安全静默（符合严格MRU规范）")

        # 测试 2: 验证零模糊兜底机制 (Strict MRU Policy Assertion)
        orig_hwnd, orig_path = em._last_explorer_hwnd, em._last_explorer_path
        try:
            # 模拟场景 A: 无记录时
            em._last_explorer_hwnd = None
            em._last_explorer_path = None
            no_rec_path = em.get_last_active_explorer_path()
            if no_rec_path is None:
                print_item("PASS", "严格MRU: 无记录静默", "无前序浏览记录时返回 None，坚决拒绝随机后台兜底")
            else:
                print_item("FAIL", "严格MRU: 无记录静默", f"错误！发生了非预期的后台兜底: {no_rec_path}")

            # 模拟场景 B: 上一个浏览窗口已关闭
            em._last_explorer_hwnd = 0xFFFFFF  # 无效句柄
            em._last_explorer_path = "D:\\InvalidDummy"
            closed_rec_path = em.get_last_active_explorer_path()
            if closed_rec_path is None:
                print_item("PASS", "严格MRU: 窗口失效静默", "上一个浏览窗口关闭时记录失效并返回 None，坚决不乱跳")
            else:
                print_item("FAIL", "严格MRU: 窗口失效静默", f"错误！窗口关闭时发生了后台兜底: {closed_rec_path}")
        finally:
            em._last_explorer_hwnd, em._last_explorer_path = orig_hwnd, orig_path

    except Exception as e:
        print_item("FAIL", "Explorer 探测异常", str(e))

# ==========================================
# 4. 文件对话框与路径注入诊断
# ==========================================
def diagnose_dialog_injector():
    print_header("4. 通用文件对话框 (#32770) 与注入器诊断")
    try:
        from core.injector import PathInjector
        injector = PathInjector()

        # 查找当前屏幕上是否存在可见的 #32770 文件对话框
        dialogs = []
        def enum_dlg(hwnd, _):
            if win32gui.GetClassName(hwnd) == '#32770':
                if not win32gui.IsWindowVisible(hwnd):
                    return
                rect = win32gui.GetWindowRect(hwnd)
                if (rect[2] - rect[0] <= 30) or (rect[3] - rect[1] <= 30):
                    return
                title = win32gui.GetWindowText(hwnd)
                edit, btn = injector.find_dialog_controls(hwnd)
                dialogs.append((hwnd, title, edit, btn))
        win32gui.EnumWindows(enum_dlg, None)

        if dialogs:
            for hwnd, title, edit, btn in dialogs:
                if edit:
                    btn_str = f" | 确认按钮 HWND: {btn}" if btn else ""
                    print_item("PASS", f"对话框 [HWND:{hwnd}]", f"标题: '{title}' | 找到编辑框 HWND: {edit}{btn_str}")
                else:
                    print_item("WARN", f"对话框 [HWND:{hwnd}]", f"标题: '{title}' | 未定位到标准 Edit 控件")
        else:
            print_item("INFO", "文件对话框检测", "当前无打开的可见 #32770 文件选择/保存对话框（可在记事本按 Ctrl+O 打开一个测试）")
    except Exception as e:
        print_item("FAIL", "注入器诊断异常", str(e))

# ==========================================
# 5. 系统开机自启动配置与自愈诊断
# ==========================================
def diagnose_autostart():
    print_header("5. 系统开机自启动 (HKCU Run) 配置诊断")
    try:
        from core.autostart import AutoStartManager
        enabled = AutoStartManager.is_autostart_enabled()
        reg_cmd = AutoStartManager.get_registered_command()
        expected_cmd = AutoStartManager.get_target_command(silent=True)

        if enabled:
            print_item("PASS", "开机自启动状态", f"已开启 (HKCU\\Software\\...\\Run)")
            print(f"         ├─ 当前注册命令: {reg_cmd}")
            print(f"         ├─ 推荐静默命令: {expected_cmd}")
            # 校验无控制台 pythonw 属性
            if "pythonw.exe" in (reg_cmd or "").lower() or getattr(sys, 'frozen', False):
                print_item("PASS", "静默运行模式", "已使用 pythonw.exe / 无控制台进程，开机不弹黑框")
            else:
                print_item("WARN", "静默运行模式", "命令中未显式使用 pythonw.exe，可能在开机时闪烁黑框")

            # 校验参数是否含 --silent
            if "--silent" in (reg_cmd or ""):
                print_item("PASS", "静默自启参数", "已携带 --silent 静默自启参数")
            else:
                print_item("WARN", "静默自启参数", "未携带 --silent 参数")

            healed, heal_msg = AutoStartManager.self_heal_if_needed()
            if healed:
                print_item("PASS", "自启动配置自愈", f"检测到异常并已自动修复: {heal_msg}")
            else:
                print_item("PASS", "路径有效性校验", "注册命令指向的二进制与脚本文件均存在有效")
        else:
            print_item("INFO", "开机自启动状态", "未开启 (可在托盘右键菜单随时勾选开启)")
            print(f"         └─ 开启时写入的命令预期为: {expected_cmd}")
    except Exception as e:
        print_item("FAIL", "自启动诊断异常", str(e))

# ==========================================
# 6. 单实例管控与多进程健康度诊断
# ==========================================
def diagnose_single_instance():
    print_header("6. 单实例管控与多进程健康度诊断")
    try:
        from core.single_instance import SingleInstanceManager, HOST_WINDOW_TITLE, WAKEUP_MSG
        import psutil

        sim = SingleInstanceManager()
        SYNCHRONIZE = 0x00100000

        # 1. 检查内核命名互斥体状态
        h_mutex = kernel32.OpenMutexW(SYNCHRONIZE, False, sim.mutex_name)
        if h_mutex:
            print_item("PASS", "内核命名互斥体", f"已由活动主实例独占持有: {sim.mutex_name}")
            kernel32.CloseHandle(h_mutex)
        else:
            print_item("INFO", "内核命名互斥体", f"当前空闲 (无常驻实例持有: {sim.mutex_name})")

        # 2. 枚举后台运行的 ZenSearch 进程
        my_pid = os.getpid()
        zen_procs = []
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if p.pid == my_pid:
                    continue
                cmdline = p.info['cmdline'] or []
                cmd_str = " ".join(cmdline).lower()
                name = (p.info['name'] or "").lower()

                if "main.py" in cmd_str or name == "zensearch.exe":
                    zen_procs.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if not zen_procs:
            print_item("INFO", "后台运行实例统计", "系统当前无常驻 ZenSearch 进程")
        elif len(zen_procs) == 1:
            p = zen_procs[0]
            print_item("PASS", "后台运行实例统计", f"仅有 1 个 ZenSearch 进程在运行 (PID: {p.pid})，符合单实例规范")
        else:
            print_item("WARN", "后台运行实例统计", f"检测到 {len(zen_procs)} 个 ZenSearch 进程正在运行，可能存在未退出的孤儿进程！")
            for p in zen_procs:
                cmd_full = " ".join(p.info['cmdline'] or [])
                print(f"         ├─ [PID: {p.pid}] 命令: {cmd_full}")

        # 3. 跨实例消息宿主窗口检测
        h_host = win32gui.FindWindow(None, HOST_WINDOW_TITLE)
        if h_host and win32gui.IsWindow(h_host):
            _, host_pid = win32process.GetWindowThreadProcessId(h_host)
            print_item("PASS", "跨实例消息宿主窗口", f"窗口句柄 [HWND:{h_host}] 正常响应 (宿主 PID: {host_pid})")
        else:
            if zen_procs:
                print_item("WARN", "跨实例消息宿主窗口", "检测到 ZenSearch 进程在运行，但未发现 ZenSearch_MessageHost 宿主窗口！")
            else:
                print_item("INFO", "跨实例消息宿主窗口", "未就绪 (当前无后台运行实例)")

        # 4. 单实例互斥机制自检 (防并发与重复启动拦截测试)
        test_app_id = "ZenSearch_DiagnosticTest"
        m1 = SingleInstanceManager(test_app_id)
        m2 = SingleInstanceManager(test_app_id)
        try:
            ok1 = m1.acquire()
            ok2 = m2.acquire()
            if ok1 and not ok2:
                print_item("PASS", "互斥防并发拦截自检", "首实例成功加锁，次实例被 100% 拦截 (ERROR_ALREADY_EXISTS)")
            else:
                print_item("FAIL", "互斥防并发拦截自检", f"互斥拦截失败: ok1={ok1}, ok2={ok2}")
        finally:
            m1.release()
            m2.release()

        # 5. 互斥锁释放与自愈测试
        m3 = SingleInstanceManager(test_app_id)
        try:
            ok3 = m3.acquire()
            if ok3:
                print_item("PASS", "互斥锁释放与自愈", "主实例释放后互斥锁恢复可用，无死锁隐患")
            else:
                print_item("FAIL", "互斥锁释放与自愈", "释放后重新获取互斥锁失败")
        finally:
            m3.release()

    except Exception as e:
        print_item("FAIL", "单实例管控诊断异常", str(e))

# ==========================================
# 7. 实时交互式按键监听器 (--watch 模式)
# ==========================================
def run_live_watch():
    print_header("7. 实时全局按键与窗口状态监控器 (--watch 模式)")
    print("说明: 此时请在系统任意界面敲击按键（如双击 Ctrl、在资源管理器打字、在对话框按 Ctrl+G）。")
    print("控制台将实时显示按键 VK 码、两次 Ctrl 间隔时间、前台窗口类名与触发判定。")
    print("按 Ctrl + C 退出监控。\n" + "-" * 60)

    from core.explorer import ExplorerManager
    em = ExplorerManager.get_instance()

    last_ctrl_time = 0.0

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    VK_CONTROL = 0x11
    VK_LCONTROL = 0xA2
    VK_RCONTROL = 0xA3
    VK_G = 0x47

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ('vkCode', wintypes.DWORD),
            ('scanCode', wintypes.DWORD),
            ('flags', wintypes.DWORD),
            ('time', wintypes.DWORD),
            ('dwExtraInfo', ctypes.c_ulonglong)
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    def hook_proc(nCode, wParam, lParam):
        nonlocal last_ctrl_time
        if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = KBDLLHOOKSTRUCT.from_address(lParam)
            vk = kb.vkCode

            fg_hwnd = win32gui.GetForegroundWindow()
            cls_name = win32gui.GetClassName(fg_hwnd) if fg_hwnd else "None"
            title = win32gui.GetWindowText(fg_hwnd) if fg_hwnd else "None"

            now = time.time()

            if vk in (VK_CONTROL, VK_LCONTROL, VK_RCONTROL):
                delta_ms = (now - last_ctrl_time) * 1000 if last_ctrl_time > 0 else 0
                if 50 < delta_ms < 350:
                    print(f"\033[92m[★ 触发双击 Ctrl]\033[0m 间隔: {delta_ms:.1f}ms | 前台: [{cls_name}] '{title}'")
                    last_ctrl_time = 0.0
                else:
                    print(f"[Ctrl 按下] 距上次: {delta_ms:.1f}ms | 等待第二次按键中...")
                    last_ctrl_time = now
            else:
                last_ctrl_time = 0.0 # 非 Ctrl 清除计数
                is_ctrl = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)

                if vk == VK_G and is_ctrl:
                    dlg = em.find_active_dialog(fg_hwnd)
                    print(f"\033[93m[Ctrl + G 触发]\033[0m 前台类名: [{cls_name}] 对话框判定: {bool(dlg)} (HWND: {dlg})")
                elif cls_name in ('CabinetWClass', 'ExploreWClass'):
                    if (0x30 <= vk <= 0x39) or (0x41 <= vk <= 0x5A) or vk == win32con.VK_SPACE:
                        char = chr(vk).lower() if (0x41 <= vk <= 0x5A) else chr(vk)
                        print(f"\033[94m[资源管理器打字]\033[0m 捕获字符: '{char}' (VK: 0x{vk:02X}) | 文件夹: '{title}'")
                else:
                    # 常规按键日志
                    pass

        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # 切换至 Default 桌面以监听真实用户输入
    h_desk = user32.OpenDesktopW('Default', 0, False, 0x10000000)
    if h_desk:
        user32.SetThreadDesktop(h_desk)

    WINEVENTPROC = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
        ctypes.c_long, ctypes.c_long, wintypes.DWORD, wintypes.DWORD
    )

    def fg_event_proc(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        if event == 0x0003 and hwnd:
            try:
                cls_name = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                if em.is_explorer_window(hwnd):
                    path = em.get_explorer_path(hwnd)
                    print(f"\033[96m[焦点切换 - 资源管理器]\033[0m HWND={hwnd} | 路径: {path} | 标题: '{title}'")
                elif em.is_dialog_window(hwnd):
                    print(f"\033[95m[焦点切换 - 文件对话框]\033[0m HWND={hwnd} | 标题: '{title}' (可按 Ctrl+G 触发路径注入)")
            except Exception:
                pass

    fg_hook_proc = WINEVENTPROC(fg_event_proc)
    h_fg_hook = user32.SetWinEventHook(
        0x0003, 0x0003, None, ctypes.cast(fg_hook_proc, ctypes.c_void_p), 0, 0, 0
    )

    c_hook = HOOKPROC(hook_proc)
    h_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, c_hook, kernel32.GetModuleHandleW(None), 0)

    if not h_hook:
        print_item("FAIL", "键盘钩子注册", f"安装失败, GetLastError: {kernel32.GetLastError()}")
        if h_fg_hook:
            user32.UnhookWinEvent(h_fg_hook)
        return

    print_item("PASS", "键盘钩子注册", "WH_KEYBOARD_LL 已成功安装至当前交互桌面。")
    if h_fg_hook:
        print_item("PASS", "焦点事件监听", "EVENT_SYSTEM_FOREGROUND 实时感知已就绪。")

    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        print("\n[退出] 监控已结束。")
    finally:
        user32.UnhookWindowsHookEx(h_hook)
        if h_fg_hook:
            user32.UnhookWinEvent(h_fg_hook)

def main():
    parser = argparse.ArgumentParser(description="ZenSearch 故障排查与诊断工具")
    parser.add_argument("--watch", action="store_true", help="启动交互式全局按键与焦点侦听模式")
    args = parser.parse_args()

    if args.watch:
        run_live_watch()
    else:
        print("ZenSearch 诊断模式启动...")
        diagnose_environment()
        diagnose_everything()
        diagnose_explorer()
        diagnose_dialog_injector()
        diagnose_autostart()
        diagnose_single_instance()
        print_header("诊断完成")
        print("提示: 若要实时监测键盘双击时序与前台捕获情况，可运行: python diagnose.py --watch\n")

if __name__ == '__main__':
    main()
