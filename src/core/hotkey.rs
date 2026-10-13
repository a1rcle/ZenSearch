use crossbeam_channel::Sender;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicPtr, Ordering};
use std::sync::Mutex;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, VK_BACK, VK_CAPITAL, VK_CONTROL, VK_DELETE, VK_DOWN, VK_ESCAPE,
    VK_F1, VK_F12, VK_F2, VK_F5, VK_LCONTROL, VK_LEFT, VK_LMENU, VK_LWIN, VK_RCONTROL,
    VK_RETURN, VK_RIGHT, VK_RMENU, VK_RWIN, VK_SPACE, VK_TAB, VK_UP,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetClassNameW, GetForegroundWindow, GetMessageW,
    PostThreadMessageW, SetWindowsHookExW, UnhookWindowsHookEx, KBDLLHOOKSTRUCT, MSG,
    WH_KEYBOARD_LL, WM_KEYDOWN, WM_QUIT, WM_SYSKEYDOWN,
};

#[derive(Debug, Clone)]
pub enum AppEvent {
    ToggleLauncher,
    DismissLauncher,
    OpenCompanion { initial_char: Option<char>, explorer_hwnd: isize },
    DismissCompanion,
    TriggerManualInjection,
    DiagnoseKeyPress { delta_ms: i64, is_double_ctrl: bool, vk: u16 },
}

static LAST_CTRL_PRESS_MS: AtomicI64 = AtomicI64::new(0);
static EVENT_SENDER: Mutex<Option<Sender<AppEvent>>> = Mutex::new(None);
static HOOK_THREAD_ID: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
static HOOK_HANDLE: AtomicPtr<std::ffi::c_void> = AtomicPtr::new(std::ptr::null_mut());
pub static WATCH_MODE: AtomicBool = AtomicBool::new(false);

pub unsafe extern "system" fn low_level_keyboard_proc(
    n_code: i32,
    w_param: WPARAM,
    l_param: LPARAM,
) -> LRESULT {
    if n_code >= 0 && (w_param.0 as u32 == WM_KEYDOWN || w_param.0 as u32 == WM_SYSKEYDOWN) {
        let kbd = *(l_param.0 as *const KBDLLHOOKSTRUCT);
        let vk = kbd.vkCode as u16;

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as i64;

        // 1. Double-click Ctrl Detection
        if vk == VK_LCONTROL.0 || vk == VK_RCONTROL.0 || vk == VK_CONTROL.0 {
            let prev = LAST_CTRL_PRESS_MS.swap(now, Ordering::Relaxed);
            let diff = now - prev;

            let is_hit = diff > 50 && diff < 350;
            if WATCH_MODE.load(Ordering::Relaxed) {
                if let Ok(lock) = EVENT_SENDER.lock() {
                    if let Some(tx) = lock.as_ref() {
                        let _ = tx.try_send(AppEvent::DiagnoseKeyPress {
                            delta_ms: diff,
                            is_double_ctrl: is_hit,
                            vk,
                        });
                    }
                }
            }

            if is_hit {
                LAST_CTRL_PRESS_MS.store(0, Ordering::Relaxed);
                if let Ok(lock) = EVENT_SENDER.lock() {
                    if let Some(tx) = lock.as_ref() {
                        let _ = tx.try_send(AppEvent::ToggleLauncher);
                    }
                }
                return LRESULT(1);
            }
        } else {
            // Any other key resets double Ctrl timer
            LAST_CTRL_PRESS_MS.store(0, Ordering::Relaxed);
        }

        // 2. Ctrl + G Manual Injection in #32770 dialog
        let ctrl_down = (GetAsyncKeyState(VK_CONTROL.0 as i32) as u16 & 0x8000) != 0;
        if ctrl_down && (vk == 0x47 || vk == 0x67) { // 'G' or 'g'
            let fg = GetForegroundWindow();
            if fg.0 != std::ptr::null_mut() {
                let mut buf = [0u16; 32];
                let len = GetClassNameW(fg, &mut buf);
                let class_name = String::from_utf16_lossy(&buf[..len as usize]);
                if class_name == "#32770" {
                    if let Ok(lock) = EVENT_SENDER.lock() {
                        if let Some(tx) = lock.as_ref() {
                            let _ = tx.try_send(AppEvent::TriggerManualInjection);
                        }
                    }
                    return LRESULT(1);
                }
            }
        }

        // 3. Explorer Type-to-Search Detection
        let fg = GetForegroundWindow();
        if fg.0 != std::ptr::null_mut() {
                let mut buf = [0u16; 64];
                let len = GetClassNameW(fg, &mut buf);
                let class_name = String::from_utf16_lossy(&buf[..len as usize]);

                if class_name == "CabinetWClass" {
                    // Ignore modifier keys and navigation/command keys
                    let is_modifier = vk == VK_LCONTROL.0
                        || vk == VK_RCONTROL.0
                        || vk == VK_LMENU.0
                        || vk == VK_RMENU.0
                        || vk == VK_LWIN.0
                        || vk == VK_RWIN.0;

                    let is_control_key = vk == VK_RETURN.0
                        || vk == VK_ESCAPE.0
                        || vk == VK_TAB.0
                        || vk == VK_DELETE.0
                        || vk == VK_BACK.0
                        || vk == VK_F2.0
                        || vk == VK_F5.0
                        || (vk >= VK_F1.0 && vk <= VK_F12.0)
                        || vk == VK_LEFT.0
                        || vk == VK_RIGHT.0
                        || vk == VK_UP.0
                        || vk == VK_DOWN.0
                        || vk == VK_CAPITAL.0;

                    let alt_down = (GetAsyncKeyState(VK_LMENU.0 as i32) as u16 & 0x8000) != 0
                        || (GetAsyncKeyState(VK_RMENU.0 as i32) as u16 & 0x8000) != 0;

                    if !is_modifier && !is_control_key && !ctrl_down && !alt_down {
                        // Check focused element, focused child window class, or active menu
                        let is_busy = is_explorer_editing_or_menu_active(fg);
                        if !is_busy {
                            let ch = match vk {
                                0x30..=0x39 => Some((b'0' + (vk - 0x30) as u8) as char),
                                0x41..=0x5A => Some((b'a' + (vk - 0x41) as u8) as char),
                                _ if vk == VK_SPACE.0 => Some(' '),
                                _ => None,
                            };

                            if let Some(c) = ch {
                                if let Ok(lock) = EVENT_SENDER.lock() {
                                    if let Some(tx) = lock.as_ref() {
                                        let _ = tx.try_send(AppEvent::OpenCompanion {
                                            initial_char: Some(c),
                                            explorer_hwnd: fg.0 as isize,
                                        });
                                    }
                                }
                                return LRESULT(1); // Eat key and route to companion bar
                            }
                        }
                    }
            }
        }
    }

    CallNextHookEx(None, n_code, w_param, l_param)
}

const GUI_INMENUMODE: u32 = 0x00000004;
const GUI_SYSTEMMENUMODE: u32 = 0x00000008;
const GUI_POPUPMENUMODE: u32 = 0x00000010;

fn is_explorer_editing_or_menu_active(exp_hwnd: HWND) -> bool {
    unsafe {
        use windows::Win32::UI::WindowsAndMessaging::{GetGUIThreadInfo, GUITHREADINFO};
        let mut gui = GUITHREADINFO {
            cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
            ..Default::default()
        };
        let thread_id = windows::Win32::UI::WindowsAndMessaging::GetWindowThreadProcessId(exp_hwnd, None);
        if GetGUIThreadInfo(thread_id, &mut gui).is_ok() {
            // 1. Check if the thread is currently in a menu loop or has an active popup menu
            // (e.g. right-click context menu, window system menu, drop-down menu)
            let in_menu = (gui.flags.0 & (GUI_INMENUMODE | GUI_POPUPMENUMODE | GUI_SYSTEMMENUMODE)) != 0;
            let has_menu_owner = gui.hwndMenuOwner.0 != std::ptr::null_mut();
            if in_menu || has_menu_owner {
                return true;
            }

            // Check if active window is a context menu (#32768)
            if gui.hwndActive.0 != std::ptr::null_mut() {
                let mut class_buf = [0u16; 32];
                let len = GetClassNameW(gui.hwndActive, &mut class_buf);
                let class_str = String::from_utf16_lossy(&class_buf[..len as usize]);
                if class_str == "#32768" {
                    return true;
                }
            }

            // 2. Check if an edit control, address bar, or search box is currently focused
            if gui.hwndFocus.0 != std::ptr::null_mut() {
                let mut class_buf = [0u16; 64];
                let len = GetClassNameW(gui.hwndFocus, &mut class_buf);
                let class_str = String::from_utf16_lossy(&class_buf[..len as usize]);
                if class_str.contains("Edit") || class_str.contains("Address") || class_str.contains("SearchBox") {
                    return true;
                }
            }
        }
    }
    false
}

pub fn start_hook_thread(sender: Sender<AppEvent>) -> std::thread::JoinHandle<()> {
    {
        let mut lock = EVENT_SENDER.lock().unwrap();
        *lock = Some(sender);
    }

    std::thread::spawn(|| unsafe {
        let thread_id = windows::Win32::System::Threading::GetCurrentThreadId();
        HOOK_THREAD_ID.store(thread_id, Ordering::SeqCst);

        let hook = SetWindowsHookExW(
            WH_KEYBOARD_LL,
            Some(low_level_keyboard_proc),
            None,
            0,
        );

        if let Ok(h) = hook {
            tracing::info!("Low-level keyboard hook successfully installed");
            HOOK_HANDLE.store(h.0 as *mut std::ffi::c_void, Ordering::SeqCst);

            let mut msg = MSG::default();
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                if msg.message == WM_QUIT {
                    break;
                }
                DispatchMessageW(&msg);
            }

            let _ = UnhookWindowsHookEx(h);
            tracing::info!("Keyboard hook uninstalled");
        } else {
            tracing::error!("Failed to install WH_KEYBOARD_LL hook");
        }
    })
}

pub fn stop_hook_thread() {
    let thread_id = HOOK_THREAD_ID.load(Ordering::SeqCst);
    if thread_id != 0 {
        unsafe {
            let _ = PostThreadMessageW(thread_id, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}
