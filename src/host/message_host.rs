use crossbeam_channel::Sender;
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::Mutex;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, PostQuitMessage,
    RegisterClassW, RegisterWindowMessageW, TranslateMessage, MSG, WINDOW_EX_STYLE,
    WNDCLASSW, WS_OVERLAPPED,
};
use crate::core::hotkey::AppEvent;

static HOST_HWND: AtomicIsize = AtomicIsize::new(0);
static EVENT_TX: Mutex<Option<Sender<AppEvent>>> = Mutex::new(None);
static WAKEUP_MSG_ID: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

pub const CLASS_NAME: PCWSTR = w!("ZenSearch_MessageHost_Class");
pub const WINDOW_NAME: PCWSTR = w!("ZenSearch_MessageHost");

unsafe extern "system" fn host_wndproc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    let wakeup_id = WAKEUP_MSG_ID.load(Ordering::Relaxed);
    if wakeup_id != 0 && msg == wakeup_id {
        tracing::info!("Received single-instance wakeup message!");
        unsafe {
            let _ = windows::Win32::UI::WindowsAndMessaging::AllowSetForegroundWindow(0xFFFFFFFF);
        }
        if let Ok(lock) = EVENT_TX.lock() {
            if let Some(tx) = lock.as_ref() {
                let _ = tx.try_send(AppEvent::ToggleLauncher);
            }
        }
        return LRESULT(0);
    }

    match msg {
        windows::Win32::UI::WindowsAndMessaging::WM_DESTROY => {
            PostQuitMessage(0);
            LRESULT(0)
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

pub fn start_message_host(tx: Sender<AppEvent>) -> std::thread::JoinHandle<()> {
    {
        let mut lock = EVENT_TX.lock().unwrap();
        *lock = Some(tx);
    }

    std::thread::spawn(|| unsafe {
        let msg_id = RegisterWindowMessageW(crate::core::single_instance::WAKEUP_MSG_NAME);
        WAKEUP_MSG_ID.store(msg_id, Ordering::Relaxed);

        let wc = WNDCLASSW {
            lpfnWndProc: Some(host_wndproc),
            lpszClassName: CLASS_NAME,
            ..Default::default()
        };

        let atom = RegisterClassW(&wc);
        if atom == 0 {
            tracing::warn!("RegisterClassW for message host atom was 0 (might already exist)");
        }

        let hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            CLASS_NAME,
            WINDOW_NAME,
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
            HOST_HWND.store(h.0 as isize, Ordering::SeqCst);
            tracing::info!("ZenSearch_MessageHost window created: {:?}", h);

            let mut msg = MSG::default();
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        } else {
            tracing::error!("Failed to create ZenSearch_MessageHost window");
        }
    })
}
