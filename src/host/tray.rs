use crossbeam_channel::Sender;
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::Mutex;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
use windows::Win32::UI::Shell::{
    Shell_NotifyIconW, NIM_ADD, NIM_DELETE, NOTIFYICONDATAW, NIF_ICON, NIF_MESSAGE, NIF_TIP,
};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::{
    AppendMenuW, CreatePopupMenu, CreateWindowExW, DefWindowProcW, DestroyMenu, DispatchMessageW,
    GetCursorPos, GetMessageW, LoadIconW, PostQuitMessage, RegisterClassW, SetForegroundWindow,
    TrackPopupMenu, TranslateMessage, HICON, HMENU, IDI_APPLICATION, MF_CHECKED, MF_SEPARATOR,
    MF_STRING, MF_UNCHECKED, MSG, TPM_BOTTOMALIGN, TPM_LEFTALIGN, TPM_RETURNCMD, WINDOW_EX_STYLE,
    WM_LBUTTONDBLCLK, WM_LBUTTONUP, WM_RBUTTONUP, WM_USER, WNDCLASSW, WS_OVERLAPPED,
};
use crate::core::hotkey::AppEvent;

const WM_TRAYICON: u32 = WM_USER + 100;
const TRAY_CLASS_NAME: PCWSTR = w!("ZenSearch_Tray_Class");
const TRAY_WINDOW_NAME: PCWSTR = w!("ZenSearch_Tray_Window");

const CMD_SHOW_LAUNCHER: u32 = 1001;
const CMD_TOGGLE_AUTOSTART: u32 = 1002;
const CMD_EXIT: u32 = 1003;

static TRAY_HWND: AtomicIsize = AtomicIsize::new(0);
static EVENT_TX: Mutex<Option<Sender<AppEvent>>> = Mutex::new(None);

unsafe extern "system" fn tray_wndproc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match msg {
        WM_TRAYICON => {
            let event = lparam.0 as u32;
            if event == WM_LBUTTONUP || event == WM_LBUTTONDBLCLK {
                if let Ok(lock) = EVENT_TX.lock() {
                    if let Some(tx) = lock.as_ref() {
                        let _ = tx.try_send(AppEvent::ToggleLauncher);
                    }
                }
            } else if event == WM_RBUTTONUP {
                show_tray_menu(hwnd);
            }
            LRESULT(0)
        }
        windows::Win32::UI::WindowsAndMessaging::WM_DESTROY => {
            remove_tray_icon(hwnd);
            PostQuitMessage(0);
            LRESULT(0)
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

unsafe fn show_tray_menu(hwnd: HWND) {
    let mut pt = POINT::default();
    let _ = GetCursorPos(&mut pt);

    let hmenu: HMENU = CreatePopupMenu().unwrap_or_default();
    if hmenu.0 == std::ptr::null_mut() {
        return;
    }

    let _ = AppendMenuW(hmenu, MF_STRING, CMD_SHOW_LAUNCHER as usize, w!("极速搜索 (双击 Ctrl)"));
    let _ = AppendMenuW(hmenu, MF_SEPARATOR, 0, PCWSTR::null());

    let autostart_state = if crate::core::autostart::is_autostart_enabled() {
        MF_CHECKED
    } else {
        MF_UNCHECKED
    };
    let _ = AppendMenuW(hmenu, MF_STRING | autostart_state, CMD_TOGGLE_AUTOSTART as usize, w!("开机自启动"));
    let _ = AppendMenuW(hmenu, MF_SEPARATOR, 0, PCWSTR::null());
    let _ = AppendMenuW(hmenu, MF_STRING, CMD_EXIT as usize, w!("退出 ZenSearch"));

    let _ = SetForegroundWindow(hwnd);
    let cmd = TrackPopupMenu(
        hmenu,
        TPM_RETURNCMD | TPM_LEFTALIGN | TPM_BOTTOMALIGN,
        pt.x,
        pt.y,
        0,
        hwnd,
        None,
    );

    let _ = DestroyMenu(hmenu);

    match cmd.0 as u32 {
        CMD_SHOW_LAUNCHER => {
            if let Ok(lock) = EVENT_TX.lock() {
                if let Some(tx) = lock.as_ref() {
                    let _ = tx.try_send(AppEvent::ToggleLauncher);
                }
            }
        }
        CMD_TOGGLE_AUTOSTART => {
            if crate::core::autostart::is_autostart_enabled() {
                let _ = crate::core::autostart::disable_registry_autostart();
            } else {
                let _ = crate::core::autostart::enable_registry_autostart();
            }
        }
        CMD_EXIT => {
            std::process::exit(0);
        }
        _ => {}
    }
}

unsafe fn add_tray_icon(hwnd: HWND) {
    let hinstance = GetModuleHandleW(None).unwrap_or_default();
    let icon = LoadIconW(hinstance, PCWSTR(1 as _))
        .or_else(|_| LoadIconW(None, IDI_APPLICATION))
        .unwrap_or(HICON(0 as _));
    let mut tip = [0u16; 128];
    let tip_text = "ZenSearch - 极速桌面启动器";
    for (i, c) in tip_text.encode_utf16().enumerate().take(127) {
        tip[i] = c;
    }

    let nid = NOTIFYICONDATAW {
        cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
        hWnd: hwnd,
        uID: 1,
        uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP,
        uCallbackMessage: WM_TRAYICON,
        hIcon: icon,
        szTip: tip,
        ..Default::default()
    };

    let _ = Shell_NotifyIconW(NIM_ADD, &nid);
}

unsafe fn remove_tray_icon(hwnd: HWND) {
    let nid = NOTIFYICONDATAW {
        cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
        hWnd: hwnd,
        uID: 1,
        ..Default::default()
    };
    let _ = Shell_NotifyIconW(NIM_DELETE, &nid);
}

pub fn start_tray_thread(tx: Sender<AppEvent>) -> std::thread::JoinHandle<()> {
    {
        let mut lock = EVENT_TX.lock().unwrap();
        *lock = Some(tx);
    }

    std::thread::spawn(|| unsafe {
        let wc = WNDCLASSW {
            lpfnWndProc: Some(tray_wndproc),
            lpszClassName: TRAY_CLASS_NAME,
            ..Default::default()
        };

        let _ = RegisterClassW(&wc);

        let hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            TRAY_CLASS_NAME,
            TRAY_WINDOW_NAME,
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            HWND(0 as _),
            None,
            None,
            None,
        );

        if let Ok(h) = hwnd {
            TRAY_HWND.store(h.0 as isize, Ordering::SeqCst);
            add_tray_icon(h);

            let mut msg = MSG::default();
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        }
    })
}
