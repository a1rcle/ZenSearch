use crossbeam_channel::Sender;
use std::sync::atomic::{AtomicI64, AtomicIsize, Ordering};
use std::sync::Mutex;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{BOOL, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
use windows::Win32::UI::Input::KeyboardAndMouse::{SetActiveWindow, SetFocus};
use windows::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, CallWindowProcW, DefWindowProcW, EnumThreadWindows, FindWindowW,
    GetForegroundWindow, GetWindowLongW, GetWindowTextW, GetWindowThreadProcessId,
    SetForegroundWindow, SetWindowLongPtrW, SetWindowLongW, SetWindowPos, GWLP_WNDPROC,
    GWL_EXSTYLE, HWND_TOPMOST, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
    WS_EX_APPWINDOW, WS_EX_TOOLWINDOW, WNDPROC,
};
use crate::core::hotkey::AppEvent;

static LAUNCHER_HWND: AtomicIsize = AtomicIsize::new(0);
static PREV_WNDPROC: AtomicIsize = AtomicIsize::new(0);
static EVENT_SENDER: Mutex<Option<Sender<AppEvent>>> = Mutex::new(None);
static LAST_SHOW_MS: AtomicI64 = AtomicI64::new(0);

static COMPANION_HWND: AtomicIsize = AtomicIsize::new(0);
static PREV_COMPANION_WNDPROC: AtomicIsize = AtomicIsize::new(0);
static LAST_COMPANION_SHOW_MS: AtomicI64 = AtomicI64::new(0);

pub fn set_event_sender(tx: Sender<AppEvent>) {
    let mut lock = EVENT_SENDER.lock().unwrap();
    *lock = Some(tx);
}

pub fn record_show_time() {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;
    LAST_SHOW_MS.store(now, Ordering::SeqCst);
}

pub fn record_companion_show_time() {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;
    LAST_COMPANION_SHOW_MS.store(now, Ordering::SeqCst);
}

pub fn get_launcher_hwnd() -> HWND {
    HWND(LAUNCHER_HWND.load(Ordering::SeqCst) as *mut _)
}

pub fn get_companion_hwnd() -> HWND {
    HWND(COMPANION_HWND.load(Ordering::SeqCst) as *mut _)
}

pub fn find_launcher_hwnd() -> Option<HWND> {
    let cached = get_launcher_hwnd();
    if cached.0 != std::ptr::null_mut() {
        return Some(cached);
    }

    unsafe {
        // 1. Try FindWindowW with title
        let title_wide: Vec<u16> = "ZenSearch_Launcher\0".encode_utf16().collect();
        if let Ok(h) = FindWindowW(PCWSTR::null(), PCWSTR(title_wide.as_ptr())) {
            if h.0 != std::ptr::null_mut() {
                LAUNCHER_HWND.store(h.0 as isize, Ordering::SeqCst);
                return Some(h);
            }
        }

        // 2. Fallback: EnumThreadWindows
        let mut found: HWND = HWND(std::ptr::null_mut());
        unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
            let mut buf = [0u16; 64];
            let len = GetWindowTextW(hwnd, &mut buf);
            let text = String::from_utf16_lossy(&buf[..len as usize]);
            if text == "ZenSearch_Launcher" {
                let ptr = lparam.0 as *mut HWND;
                *ptr = hwnd;
                return BOOL(0);
            }
            BOOL(1)
        }

        let _ = EnumThreadWindows(
            GetCurrentThreadId(),
            Some(enum_proc),
            LPARAM(&mut found as *mut HWND as _),
        );

        if found.0 != std::ptr::null_mut() {
            LAUNCHER_HWND.store(found.0 as isize, Ordering::SeqCst);
            Some(found)
        } else {
            None
        }
    }
}

pub fn find_companion_hwnd() -> Option<HWND> {
    let cached = get_companion_hwnd();
    if cached.0 != std::ptr::null_mut() {
        return Some(cached);
    }

    unsafe {
        // 1. Try FindWindowW with title
        let title_wide: Vec<u16> = "ZenSearch_Companion\0".encode_utf16().collect();
        if let Ok(h) = FindWindowW(PCWSTR::null(), PCWSTR(title_wide.as_ptr())) {
            if h.0 != std::ptr::null_mut() {
                COMPANION_HWND.store(h.0 as isize, Ordering::SeqCst);
                return Some(h);
            }
        }

        // 2. Fallback: EnumThreadWindows
        let mut found: HWND = HWND(std::ptr::null_mut());
        unsafe extern "system" fn enum_comp_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
            let mut buf = [0u16; 64];
            let len = GetWindowTextW(hwnd, &mut buf);
            let text = String::from_utf16_lossy(&buf[..len as usize]);
            if text == "ZenSearch_Companion" {
                let ptr = lparam.0 as *mut HWND;
                *ptr = hwnd;
                return BOOL(0);
            }
            BOOL(1)
        }

        let _ = EnumThreadWindows(
            GetCurrentThreadId(),
            Some(enum_comp_proc),
            LPARAM(&mut found as *mut HWND as _),
        );

        if found.0 != std::ptr::null_mut() {
            COMPANION_HWND.store(found.0 as isize, Ordering::SeqCst);
            Some(found)
        } else {
            None
        }
    }
}

pub fn setup_launcher_window(hwnd: HWND, tx: Sender<AppEvent>) {
    LAUNCHER_HWND.store(hwnd.0 as isize, Ordering::SeqCst);
    set_event_sender(tx);

    unsafe {
        // Apply WS_EX_TOOLWINDOW and strip WS_EX_APPWINDOW (Floating Tool Window Overlay)
        let mut ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
        ex_style = (ex_style | WS_EX_TOOLWINDOW.0) & !WS_EX_APPWINDOW.0;
        SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style as i32);

        let _ = SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        );

        // Subclass to intercept WM_ACTIVATE (WA_INACTIVE)
        let existing = PREV_WNDPROC.load(Ordering::SeqCst);
        if existing == 0 {
            let old_proc = SetWindowLongPtrW(
                hwnd,
                GWLP_WNDPROC,
                launcher_subclass_proc as *const () as usize as isize,
            );
            if old_proc != 0 {
                PREV_WNDPROC.store(old_proc, Ordering::SeqCst);
            }
        }
    }
}

pub fn setup_companion_window(hwnd: HWND, tx: Sender<AppEvent>) {
    COMPANION_HWND.store(hwnd.0 as isize, Ordering::SeqCst);
    set_event_sender(tx);

    unsafe {
        // Apply WS_EX_TOOLWINDOW and strip WS_EX_APPWINDOW (Floating Tool Window Overlay)
        let mut ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
        ex_style = (ex_style | WS_EX_TOOLWINDOW.0) & !WS_EX_APPWINDOW.0;
        SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style as i32);

        let _ = SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        );

        // Subclass to intercept WM_ACTIVATE (WA_INACTIVE)
        let existing = PREV_COMPANION_WNDPROC.load(Ordering::SeqCst);
        if existing == 0 {
            let old_proc = SetWindowLongPtrW(
                hwnd,
                GWLP_WNDPROC,
                companion_subclass_proc as *const () as usize as isize,
            );
            if old_proc != 0 {
                PREV_COMPANION_WNDPROC.store(old_proc, Ordering::SeqCst);
            }
        }
    }
}

unsafe extern "system" fn launcher_subclass_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    const WM_ACTIVATE: u32 = 0x0006;
    const WA_INACTIVE: u32 = 0;

    if msg == WM_ACTIVATE {
        let active_code = (wparam.0 & 0xFFFF) as u32;
        if active_code == WA_INACTIVE {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as i64;
            let show_time = LAST_SHOW_MS.load(Ordering::SeqCst);

            // Avoid dismissing during initial 200ms wake-up activation window
            if now > show_time + 200 {
                if let Ok(lock) = EVENT_SENDER.lock() {
                    if let Some(tx) = lock.as_ref() {
                        let _ = tx.try_send(AppEvent::DismissLauncher);
                    }
                }
            }
        }
    }

    let prev = PREV_WNDPROC.load(Ordering::SeqCst);
    if prev != 0 {
        let prev_fn: WNDPROC = std::mem::transmute(prev);
        CallWindowProcW(prev_fn, hwnd, msg, wparam, lparam)
    } else {
        DefWindowProcW(hwnd, msg, wparam, lparam)
    }
}

unsafe extern "system" fn companion_subclass_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    const WM_ACTIVATE: u32 = 0x0006;
    const WA_INACTIVE: u32 = 0;

    if msg == WM_ACTIVATE {
        let active_code = (wparam.0 & 0xFFFF) as u32;
        if active_code == WA_INACTIVE {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as i64;
            let show_time = LAST_COMPANION_SHOW_MS.load(Ordering::SeqCst);

            // Avoid dismissing during initial 200ms wake-up activation window
            if now > show_time + 200 {
                if let Ok(lock) = EVENT_SENDER.lock() {
                    if let Some(tx) = lock.as_ref() {
                        let _ = tx.try_send(AppEvent::DismissCompanion);
                    }
                }
            }
        }
    }

    let prev = PREV_COMPANION_WNDPROC.load(Ordering::SeqCst);
    if prev != 0 {
        let prev_fn: WNDPROC = std::mem::transmute(prev);
        CallWindowProcW(prev_fn, hwnd, msg, wparam, lparam)
    } else {
        DefWindowProcW(hwnd, msg, wparam, lparam)
    }
}

pub fn handle_foreground_changed(new_fg: HWND) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;

    // 1. Launcher check
    let launcher = get_launcher_hwnd();
    if launcher.0 != std::ptr::null_mut() && new_fg != launcher {
        let show_time = LAST_SHOW_MS.load(Ordering::SeqCst);
        if now > show_time + 200 {
            if let Ok(lock) = EVENT_SENDER.lock() {
                if let Some(tx) = lock.as_ref() {
                    let _ = tx.try_send(AppEvent::DismissLauncher);
                }
            }
        }
    }

    // 2. Companion check
    let companion = get_companion_hwnd();
    if companion.0 != std::ptr::null_mut() && new_fg != companion {
        let show_time = LAST_COMPANION_SHOW_MS.load(Ordering::SeqCst);
        if now > show_time + 200 {
            if let Ok(lock) = EVENT_SENDER.lock() {
                if let Some(tx) = lock.as_ref() {
                    let _ = tx.try_send(AppEvent::DismissCompanion);
                }
            }
        }
    }
}

pub fn force_foreground_and_focus(hwnd: HWND) {
    if hwnd == get_companion_hwnd() {
        record_companion_show_time();
    } else {
        record_show_time();
    }
    unsafe {
        let fg_hwnd = GetForegroundWindow();
        let fg_thread = if fg_hwnd.0 != std::ptr::null_mut() {
            GetWindowThreadProcessId(fg_hwnd, None)
        } else {
            0
        };
        let cur_thread = GetCurrentThreadId();

        if fg_thread != 0 && fg_thread != cur_thread {
            let _ = AttachThreadInput(cur_thread, fg_thread, true);
        }

        let _ = BringWindowToTop(hwnd);
        let _ = SetForegroundWindow(hwnd);
        let _ = SetActiveWindow(hwnd);

        if fg_thread != 0 && fg_thread != cur_thread {
            let _ = AttachThreadInput(cur_thread, fg_thread, false);
        }

        let _ = SetFocus(hwnd);
    }
}
