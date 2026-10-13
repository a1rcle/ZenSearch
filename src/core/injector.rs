use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::sync::atomic::{AtomicIsize, AtomicU32, Ordering};
use std::sync::Mutex;
use std::thread::sleep;
use std::time::Duration;
use windows::Win32::Foundation::{BOOL, HWND, LPARAM, WPARAM};
use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
use windows::Win32::UI::Accessibility::{SetWinEventHook, UnhookWinEvent, HWINEVENTHOOK};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, IsWindowEnabled, SetFocus, VK_LBUTTON, VK_RETURN,
};
use windows::Win32::UI::WindowsAndMessaging::{
    DispatchMessageW, EnumChildWindows, GetClassNameW, GetDlgCtrlID, GetMessageW,
    GetWindowThreadProcessId, IsWindow, IsWindowVisible, PostMessageW, PostThreadMessageW,
    SendMessageTimeoutW, TranslateMessage, EVENT_SYSTEM_FOREGROUND, MSG, SMTO_ABORTIFHUNG,
    SMTO_NORMAL, WM_GETTEXT, WM_GETTEXTLENGTH, WM_KEYDOWN, WM_KEYUP, WM_QUIT, WM_SETTEXT,
};

#[derive(Debug, Clone, Default)]
pub struct FocusTracker {
    pub last_explorer_hwnd: Option<isize>,
    pub last_explorer_path: Option<String>,
    pub prev_foreground_hwnd: Option<isize>,
}

pub static TRACKER: Mutex<FocusTracker> = Mutex::new(FocusTracker {
    last_explorer_hwnd: None,
    last_explorer_path: None,
    prev_foreground_hwnd: None,
});

static HOOK_HANDLE: AtomicIsize = AtomicIsize::new(0);
static INJECTOR_THREAD_ID: AtomicU32 = AtomicU32::new(0);

pub unsafe extern "system" fn win_event_proc(
    _h_win_event_hook: HWINEVENTHOOK,
    event: u32,
    hwnd: HWND,
    _id_object: i32,
    _id_child: i32,
    _id_event_thread: u32,
    _dwms_event_time: u32,
) {
    if hwnd.0 == std::ptr::null_mut() {
        return;
    }

    if event == EVENT_SYSTEM_FOREGROUND {
        crate::core::window_helper::handle_foreground_changed(hwnd);

        let mut class_buf = [0u16; 64];
        let len = GetClassNameW(hwnd, &mut class_buf);
        let class_name = String::from_utf16_lossy(&class_buf[..len as usize]);

        let hwnd_raw = hwnd.0 as isize;

        if class_name == "CabinetWClass" || class_name == "ExploreWClass" {
            // Query Explorer path outside the mutex lock to prevent STA COM re-entrancy deadlocks
            let maybe_path = crate::core::explorer::get_explorer_path(hwnd);
            if let Ok(mut tracker) = TRACKER.lock() {
                if let Some(path) = maybe_path {
                    tracker.last_explorer_hwnd = Some(hwnd_raw);
                    tracker.last_explorer_path = Some(path);
                }
                tracker.prev_foreground_hwnd = Some(hwnd_raw);
            }
        } else if class_name == "#32770" {
            let mut injection_target: Option<(isize, String)> = None;
            if let Ok(mut tracker) = TRACKER.lock() {
                // Check transition chain: was previous foreground window exactly the tracked Explorer?
                if tracker.prev_foreground_hwnd.is_some()
                    && tracker.prev_foreground_hwnd == tracker.last_explorer_hwnd
                {
                    if let (Some(exp_hwnd_val), Some(target_path)) =
                        (tracker.last_explorer_hwnd, tracker.last_explorer_path.clone())
                    {
                        injection_target = Some((exp_hwnd_val, target_path));
                    }
                }
                // Always update prev_foreground_hwnd so internal #32770 focus events don't re-trigger
                tracker.prev_foreground_hwnd = Some(hwnd_raw);
            }

            if let Some((exp_hwnd_val, target_path)) = injection_target {
                let exp_hwnd = HWND(exp_hwnd_val as *mut _);
                if IsWindow(exp_hwnd).as_bool() {
                    tracing::info!(
                        "Valid Explorer -> #32770 transition detected! Target path: {}",
                        target_path
                    );
                    std::thread::spawn(move || {
                        inject_path_to_dialog(HWND(hwnd_raw as *mut _), &target_path);
                    });
                }
            }
        } else if let Ok(mut tracker) = TRACKER.lock() {
            tracker.prev_foreground_hwnd = Some(hwnd_raw);
        }
    }
}

pub fn start_injector_hook() -> std::thread::JoinHandle<()> {
    std::thread::spawn(|| unsafe {
        let thread_id = windows::Win32::System::Threading::GetCurrentThreadId();
        INJECTOR_THREAD_ID.store(thread_id, Ordering::SeqCst);

        let hook = SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            None,
            Some(win_event_proc),
            0,
            0,
            0,
        );

        if hook.0 != std::ptr::null_mut() {
            HOOK_HANDLE.store(hook.0 as isize, Ordering::SeqCst);
            tracing::info!("WinEventHook for path injection successfully installed on background thread");

            let mut msg = MSG::default();
            while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                if msg.message == WM_QUIT {
                    break;
                }
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }

            let handle = HOOK_HANDLE.swap(0, Ordering::SeqCst);
            if handle != 0 {
                let _ = UnhookWinEvent(HWINEVENTHOOK(handle as *mut _));
                tracing::info!("WinEventHook uninstalled");
            }
        } else {
            tracing::error!("Failed to install SetWinEventHook for path injection");
        }
    })
}

pub fn stop_injector_hook() {
    let thread_id = INJECTOR_THREAD_ID.swap(0, Ordering::SeqCst);
    if thread_id != 0 {
        unsafe {
            let _ = PostThreadMessageW(thread_id, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}

pub fn find_dialog_filename_edit(dialog_hwnd: HWND) -> Option<HWND> {
    struct SearchContext {
        found: Option<HWND>,
    }

    unsafe extern "system" fn enum_child(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let ctx = &mut *(lparam.0 as *mut SearchContext);

        if !IsWindowVisible(hwnd).as_bool() || !IsWindowEnabled(hwnd).as_bool() {
            return BOOL(1);
        }

        let mut class_buf = [0u16; 64];
        let len = GetClassNameW(hwnd, &mut class_buf);
        let class_name = String::from_utf16_lossy(&class_buf[..len as usize]);

        let ctrl_id = GetDlgCtrlID(hwnd);

        // Exclude search box and top address bar
        if class_name.contains("SearchEditBox") || ctrl_id == 41477 {
            return BOOL(1);
        }

        if class_name == "Edit" {
            // Standard Common Dialog ID 0x47c (1148) or modern 1001 or general editable Edit
            if ctrl_id == 0x47c || ctrl_id == 1001 || ctrl_id == 1152 {
                ctx.found = Some(hwnd);
                return BOOL(0); // Stop enum
            }
            if ctx.found.is_none() {
                ctx.found = Some(hwnd);
            }
        }

        BOOL(1)
    }

    let mut ctx = SearchContext { found: None };
    unsafe {
        let _ = EnumChildWindows(
            dialog_hwnd,
            Some(enum_child),
            LPARAM(&mut ctx as *mut SearchContext as isize),
        );
    }

    ctx.found
}

pub fn get_window_text(hwnd: HWND) -> String {
    unsafe {
        let mut len_res: usize = 0;
        let ok = SendMessageTimeoutW(
            hwnd,
            WM_GETTEXTLENGTH,
            WPARAM(0),
            LPARAM(0),
            SMTO_ABORTIFHUNG | SMTO_NORMAL,
            200,
            Some(&mut len_res),
        );
        if ok.0 != 0 && len_res > 0 && len_res < 32768 {
            let mut buf = vec![0u16; len_res + 1];
            let mut copied: usize = 0;
            let ok2 = SendMessageTimeoutW(
                hwnd,
                WM_GETTEXT,
                WPARAM(buf.len()),
                LPARAM(buf.as_mut_ptr() as isize),
                SMTO_ABORTIFHUNG | SMTO_NORMAL,
                200,
                Some(&mut copied),
            );
            if ok2.0 != 0 && copied > 0 {
                return String::from_utf16_lossy(&buf[..copied.min(len_res)]);
            }
        }
    }
    String::new()
}

pub fn get_dialog_current_path(dialog_hwnd: HWND) -> Option<String> {
    struct ToolbarContext {
        address: Option<String>,
    }

    unsafe extern "system" fn enum_toolbar(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let ctx = &mut *(lparam.0 as *mut ToolbarContext);
        let mut class_buf = [0u16; 64];
        let len = GetClassNameW(hwnd, &mut class_buf);
        let class_name = String::from_utf16_lossy(&class_buf[..len as usize]);

        if class_name == "ToolbarWindow32" || class_name == "Breadcrumb Parent" {
            let text = get_window_text(hwnd);
            // Addresses in ToolbarWindow32 usually formatted as "Address: C:\Target"
            if text.contains(':') || text.contains('\\') {
                let clean = text.replace("Address: ", "").replace("地址: ", "");
                if !clean.trim().is_empty() {
                    ctx.address = Some(clean.trim().to_string());
                    return BOOL(0);
                }
            }
        }
        BOOL(1)
    }

    let mut ctx = ToolbarContext { address: None };
    unsafe {
        let _ = EnumChildWindows(
            dialog_hwnd,
            Some(enum_toolbar),
            LPARAM(&mut ctx as *mut ToolbarContext as isize),
        );
    }

    ctx.address
}

fn wait_for_mouse_release() {
    let mut waited_ms = 0;
    unsafe {
        while (GetAsyncKeyState(VK_LBUTTON.0 as i32) as u16 & 0x8000) != 0 && waited_ms < 400 {
            sleep(Duration::from_millis(10));
            waited_ms += 10;
        }
    }
    // Allow brief settle window for DirectUIHWND / navigation bar to finish WM_LBUTTONUP & focus transition
    sleep(Duration::from_millis(35));
}

fn focus_dialog_edit(dialog_hwnd: HWND, edit_hwnd: HWND) {
    const EM_SETSEL: u32 = 0x00B1;
    unsafe {
        let cur_thread = GetCurrentThreadId();
        let dlg_thread = GetWindowThreadProcessId(dialog_hwnd, None);
        let attached = if dlg_thread != 0 && dlg_thread != cur_thread {
            AttachThreadInput(cur_thread, dlg_thread, true).as_bool()
        } else {
            false
        };

        let _ = SetFocus(edit_hwnd);

        if attached {
            let _ = AttachThreadInput(cur_thread, dlg_thread, false);
        }

        let mut res: usize = 0;
        let _ = SendMessageTimeoutW(
            edit_hwnd,
            EM_SETSEL,
            WPARAM(0),
            LPARAM(-1),
            SMTO_ABORTIFHUNG | SMTO_NORMAL,
            100,
            Some(&mut res),
        );
    }
}

pub fn inject_path_to_dialog(dialog_hwnd: HWND, target_path: &str) -> bool {
    let edit_hwnd = match find_dialog_filename_edit(dialog_hwnd) {
        Some(h) => h,
        None => {
            tracing::warn!("Target filename Edit control not found in dialog {:?}", dialog_hwnd);
            return false;
        }
    };

    let norm_target = target_path.trim_end_matches('\\').to_lowercase();

    // 1. Check idempotency: if already in the target directory, skip to avoid flicker
    if let Some(curr_path) = get_dialog_current_path(dialog_hwnd) {
        let norm_curr = curr_path.trim_end_matches('\\').to_lowercase();
        if norm_curr == norm_target {
            tracing::info!("Dialog is already in target directory ({}), skipping injection", curr_path);
            return true;
        }
    }

    // 2. Backup current filename text before mouse click settlement overwrites it
    let initial_text = get_window_text(edit_hwnd);
    let original_filename = if !initial_text.contains('\\') && !initial_text.contains(':') {
        initial_text
    } else {
        String::new()
    };

    // 3. Wait for physical mouse button release and child control click settlement
    wait_for_mouse_release();

    // 4. Normalize path with trailing backslash so Windows dialog navigates into directory
    let mut normalized_path = target_path.to_string();
    if !normalized_path.ends_with('\\') {
        normalized_path.push('\\');
    }

    let wide_path: Vec<u16> = OsStr::new(&normalized_path)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    // 5. Execute injection with explicit focus lock and loopback verification (up to 3 attempts)
    let mut navigated = false;
    for attempt in 1..=3 {
        let current_edit = find_dialog_filename_edit(dialog_hwnd).unwrap_or(edit_hwnd);
        focus_dialog_edit(dialog_hwnd, current_edit);

        unsafe {
            let mut set_res: usize = 0;
            let ok = SendMessageTimeoutW(
                current_edit,
                WM_SETTEXT,
                WPARAM(0),
                LPARAM(wide_path.as_ptr() as isize),
                SMTO_ABORTIFHUNG | SMTO_NORMAL,
                200,
                Some(&mut set_res),
            );
            if ok.0 == 0 {
                tracing::warn!(
                    "SendMessageTimeoutW(WM_SETTEXT) timed out or failed on attempt {}",
                    attempt
                );
                continue;
            }

            // Ensure focus remains locked on current_edit when VK_RETURN is dispatched
            focus_dialog_edit(dialog_hwnd, current_edit);
            let _ = PostMessageW(current_edit, WM_KEYDOWN, WPARAM(VK_RETURN.0 as usize), LPARAM(0));
            let _ = PostMessageW(current_edit, WM_KEYUP, WPARAM(VK_RETURN.0 as usize), LPARAM(0));
        }

        // Wait for dialog message queue to navigate and verify via address bar loopback
        sleep(Duration::from_millis(90));
        if let Some(curr_path) = get_dialog_current_path(dialog_hwnd) {
            if curr_path.trim_end_matches('\\').to_lowercase() == norm_target {
                navigated = true;
                break;
            }
        } else {
            // Fallback for legacy dialogs without ToolbarWindow32
            navigated = true;
            break;
        }
    }

    // 6. Restore original filename (or clear residual injected path if originally empty)
    let final_edit = find_dialog_filename_edit(dialog_hwnd).unwrap_or(edit_hwnd);
    let current_edit_text = get_window_text(final_edit);
    let should_restore = !original_filename.is_empty()
        || current_edit_text.contains('\\')
        || current_edit_text.contains(':');

    if should_restore {
        let wide_name: Vec<u16> = OsStr::new(&original_filename)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        unsafe {
            let mut set_res: usize = 0;
            let _ = SendMessageTimeoutW(
                final_edit,
                WM_SETTEXT,
                WPARAM(0),
                LPARAM(wide_name.as_ptr() as isize),
                SMTO_ABORTIFHUNG | SMTO_NORMAL,
                200,
                Some(&mut set_res),
            );
        }
    }

    if navigated {
        tracing::info!("Successfully injected path into dialog: {}", normalized_path);
    } else {
        tracing::warn!(
            "Dialog path injection finished without confirming target address: {}",
            normalized_path
        );
    }
    navigated
}

pub fn trigger_manual_injection() -> bool {
    let fg = unsafe { windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow() };
    if fg.0 != std::ptr::null_mut() {
        let mut class_buf = [0u16; 32];
        let len = unsafe { GetClassNameW(fg, &mut class_buf) };
        let class_name = String::from_utf16_lossy(&class_buf[..len as usize]);

        if class_name == "#32770" {
            let maybe_target = TRACKER
                .lock()
                .ok()
                .and_then(|tracker| tracker.last_explorer_path.clone());
            if let Some(target_path) = maybe_target {
                return inject_path_to_dialog(fg, &target_path);
            }
        }
    }
    false
}
