use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::sync::Mutex;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS, LPARAM, WPARAM};
use windows::Win32::System::Threading::CreateMutexW;
use windows::Win32::UI::WindowsAndMessaging::{
    FindWindowW, PostMessageW, RegisterWindowMessageW, SetForegroundWindow,
};

static MUTEX_HANDLE: Mutex<Option<isize>> = Mutex::new(None);

pub const WAKEUP_MSG_NAME: PCWSTR = w!("ZenSearch_Wakeup_Broadcast_Message");

pub fn get_wakeup_message_id() -> u32 {
    unsafe { RegisterWindowMessageW(WAKEUP_MSG_NAME) }
}

pub fn acquire() -> bool {
    let username = std::env::var("USERNAME").unwrap_or_else(|_| "User".to_string());
    let mutex_name = format!("Local\\ZenSearch_SingleInstance_Mutex_{}", username);
    let wide_name: Vec<u16> = OsStr::new(&mutex_name)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    unsafe {
        let handle = CreateMutexW(None, true, PCWSTR(wide_name.as_ptr()));
        if let Ok(h) = handle {
            if GetLastError() == ERROR_ALREADY_EXISTS {
                tracing::warn!("Another ZenSearch instance is already running!");
                return false;
            }
            let mut lock = MUTEX_HANDLE.lock().unwrap();
            *lock = Some(h.0 as isize);
            true
        } else {
            tracing::error!("Failed to create single instance mutex");
            true
        }
    }
}

pub fn activate_existing_instance() {
    unsafe {
        let host_name = w!("ZenSearch_MessageHost");
        if let Ok(hwnd) = FindWindowW(host_name, PCWSTR::null()) {
            if hwnd.0 != std::ptr::null_mut() {
                tracing::info!("Found existing ZenSearch instance host window: {:?}", hwnd);
                let msg_id = get_wakeup_message_id();
                if msg_id != 0 {
                    let _ = PostMessageW(hwnd, msg_id, WPARAM(0), LPARAM(0));
                }
                let _ = SetForegroundWindow(hwnd);
            }
        } else {
            tracing::warn!("Existing instance window not found.");
        }
    }
}
