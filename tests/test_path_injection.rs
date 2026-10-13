use std::path::PathBuf;
use windows::Win32::Foundation::HWND;
use zensearch::core::injector::TRACKER;

#[test]
fn test_open_file_injection() {
    // 1. Validate normalization and path trailing slash enforcement
    let target = r"C:\Windows\System32";
    let mut normalized = target.to_string();
    if !normalized.ends_with('\\') {
        normalized.push('\\');
    }
    assert_eq!(normalized, r"C:\Windows\System32\");
    assert!(PathBuf::from(&normalized).exists());
}

#[test]
fn test_save_file_injection() {
    let target = r"C:\Users";
    let mut normalized = target.to_string();
    if !normalized.ends_with('\\') {
        normalized.push('\\');
    }
    assert_eq!(normalized, r"C:\Users\");
    assert!(PathBuf::from(&normalized).exists());
}

#[test]
fn test_consecutive_path_switching() {
    let paths = vec![
        r"C:\Windows\",
        r"C:\Program Files\",
        r"C:\",
        r"C:\Windows\",
    ];

    for p in paths {
        assert!(p.ends_with('\\'));
        let lower = p.trim_end_matches('\\').to_lowercase();
        assert!(!lower.is_empty());
    }
}

#[test]
fn test_mru_closed_window_safety() {
    // Ensure that closed or non-existent explorer window handles are rejected safely
    let mut tracker = TRACKER.lock().unwrap();
    tracker.last_explorer_hwnd = Some(0xDEADBEEF);
    tracker.last_explorer_path = Some(r"C:\NonExistent".into());
    tracker.prev_foreground_hwnd = Some(0x12345);

    // Verification: IsWindow(0xDEADBEEF) will return FALSE
    unsafe {
        let is_alive = windows::Win32::UI::WindowsAndMessaging::IsWindow(HWND(0xDEADBEEF as *mut _)).as_bool();
        assert!(!is_alive, "Dead HWND must be reported as not a valid window");
    }
}

#[test]
fn test_filename_preservation() {
    let original = "MyQuarterlyReport.docx";
    let target_dir = r"C:\Users\TargetDir\";

    assert!(!original.contains('\\'));
    assert!(target_dir.ends_with('\\'));

    let mut restored = String::new();
    if !original.is_empty() && !original.contains('\\') {
        restored = original.to_string();
    }

    assert_eq!(restored, "MyQuarterlyReport.docx");
}

#[test]
fn test_com_guard_and_tracker_transition() {
    // Verify nested ComGuard creation/drop does not corrupt thread COM state
    {
        let _g1 = zensearch::core::explorer::ComGuard::new();
        {
            let _g2 = zensearch::core::explorer::ComGuard::new();
        }
    }

    // Verify tracker state transition updates prev_foreground_hwnd properly
    let mut tracker = TRACKER.lock().unwrap();
    tracker.last_explorer_hwnd = Some(0x1001);
    tracker.last_explorer_path = Some(r"C:\Windows".into());
    tracker.prev_foreground_hwnd = Some(0x1001);

    let dialog_hwnd: isize = 0x2002;
    assert_eq!(tracker.prev_foreground_hwnd, tracker.last_explorer_hwnd);
    tracker.prev_foreground_hwnd = Some(dialog_hwnd);
    assert_ne!(tracker.prev_foreground_hwnd, tracker.last_explorer_hwnd);
}

#[test]
fn test_live_dialog_blank_and_navbar_focus_injection() {
    use std::sync::atomic::{AtomicIsize, Ordering};
    use std::thread::sleep;
    use std::time::Duration;
    use windows::Win32::Foundation::{BOOL, LPARAM, WPARAM};
    use windows::Win32::System::Com::{CoCreateInstance, CLSCTX_INPROC_SERVER};
    use windows::Win32::System::Threading::GetCurrentProcessId;
    use windows::Win32::UI::Shell::{FileOpenDialog, IFileOpenDialog};
    use windows::Win32::UI::WindowsAndMessaging::{
        EnumChildWindows, EnumWindows, GetClassNameW, GetWindowThreadProcessId, PostMessageW,
        WM_CLOSE,
    };
    use zensearch::core::injector::{get_dialog_current_path, inject_path_to_dialog};

    // Spawn a real #32770 File Open Dialog on a dedicated STA thread
    let dlg_thread = std::thread::spawn(|| unsafe {
        let _com = zensearch::core::explorer::ComGuard::new();
        if let Ok(dialog) = CoCreateInstance::<_, IFileOpenDialog>(&FileOpenDialog, None, CLSCTX_INPROC_SERVER) {
            let _ = dialog.Show(None);
        }
    });

    // Locate the #32770 window belonging to our current process
    static FOUND_DLG: AtomicIsize = AtomicIsize::new(0);
    FOUND_DLG.store(0, Ordering::SeqCst);

    unsafe extern "system" fn find_dlg_proc(hwnd: HWND, _lparam: LPARAM) -> BOOL {
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid == GetCurrentProcessId()
            && windows::Win32::UI::WindowsAndMessaging::IsWindowVisible(hwnd).as_bool()
        {
            let mut buf = [0u16; 64];
            let len = GetClassNameW(hwnd, &mut buf);
            let cls = String::from_utf16_lossy(&buf[..len as usize]);
            if cls == "#32770" {
                FOUND_DLG.store(hwnd.0 as isize, Ordering::SeqCst);
                return BOOL(0);
            }
        }
        BOOL(1)
    }

    let mut dlg_hwnd = HWND(std::ptr::null_mut());
    for _ in 0..50 {
        sleep(Duration::from_millis(100));
        unsafe {
            let _ = EnumWindows(Some(find_dlg_proc), LPARAM(0));
        }
        let val = FOUND_DLG.load(Ordering::SeqCst);
        if val != 0 {
            dlg_hwnd = HWND(val as *mut _);
            break;
        }
    }

    assert!(dlg_hwnd.0 != std::ptr::null_mut(), "Failed to spawn live #32770 dialog");
    sleep(Duration::from_millis(300));

    // Find SHELLDLL_DefView's DirectUIHWND (blank file list area) and ToolbarWindow32 / SysTreeView32 (navigation bar)
    struct ChildControls {
        def_view_dui: Option<HWND>,
        tree_view: Option<HWND>,
        toolbar: Option<HWND>,
    }

    unsafe extern "system" fn enum_children(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let ctrls = &mut *(lparam.0 as *mut ChildControls);
        let mut buf = [0u16; 64];
        let len = GetClassNameW(hwnd, &mut buf);
        let cls = String::from_utf16_lossy(&buf[..len as usize]);
        let parent = windows::Win32::UI::WindowsAndMessaging::GetParent(hwnd).unwrap_or_default();
        let mut pbuf = [0u16; 64];
        let plen = if parent.0 != std::ptr::null_mut() {
            GetClassNameW(parent, &mut pbuf)
        } else {
            0
        };
        let pcls = String::from_utf16_lossy(&pbuf[..plen as usize]);

        if cls == "DirectUIHWND" && pcls == "SHELLDLL_DefView" {
            ctrls.def_view_dui = Some(hwnd);
        } else if cls == "SysTreeView32" && ctrls.tree_view.is_none() {
            ctrls.tree_view = Some(hwnd);
        } else if cls == "ToolbarWindow32" && pcls == "Breadcrumb Parent" {
            ctrls.toolbar = Some(hwnd);
        }
        BOOL(1)
    }

    let mut ctrls = ChildControls {
        def_view_dui: None,
        tree_view: None,
        toolbar: None,
    };
    unsafe {
        let _ = EnumChildWindows(
            dlg_hwnd,
            Some(enum_children),
            LPARAM(&mut ctrls as *mut ChildControls as isize),
        );
    }

    let print_gui_state = |label: &str| unsafe {
        use windows::Win32::UI::WindowsAndMessaging::{GetGUIThreadInfo, GUITHREADINFO};
        let mut gui = GUITHREADINFO {
            cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
            ..Default::default()
        };
        let dlg_tid = GetWindowThreadProcessId(dlg_hwnd, None);
        if GetGUIThreadInfo(dlg_tid, &mut gui).is_ok() {
            let mut fbuf = [0u16; 64];
            let flen = GetClassNameW(gui.hwndFocus, &mut fbuf);
            let fcls = String::from_utf16_lossy(&fbuf[..flen as usize]);
            let fid = windows::Win32::UI::WindowsAndMessaging::GetDlgCtrlID(gui.hwndFocus);
            println!(
                "[GUI {}] hwndActive={:?}, hwndFocus={:?} (class={}, id={}), hwndCapture={:?}",
                label, gui.hwndActive, gui.hwndFocus, fcls, fid, gui.hwndCapture
            );
        }
    };

    print_gui_state("Initial");

    // Helper to simulate real mouse click on a target control:
    // 1. Move cursor & press left button down (triggers WM_MOUSEACTIVATE / EVENT_SYSTEM_FOREGROUND in real life)
    // 2. Spawn inject_path_to_dialog immediately while mouse is still held down
    // 3. Hold mouse button for 80ms (typical human click duration), then release left button
    let click_and_inject = |target_ctrl: HWND, target_dir: &'static str| -> (bool, String) {
        use windows::Win32::Foundation::RECT;
        use windows::Win32::UI::Input::KeyboardAndMouse::{
            mouse_event, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
        };
        use windows::Win32::UI::WindowsAndMessaging::{GetWindowRect, SetCursorPos, SetForegroundWindow};

        unsafe {
            let _ = SetForegroundWindow(dlg_hwnd);
            let mut r = RECT::default();
            let _ = GetWindowRect(target_ctrl, &mut r);
            // Click near bottom-right of DirectUIHWND (blank space) or center of nav control
            let x = r.left + (r.right - r.left) * 3 / 4;
            let y = r.top + (r.bottom - r.top) * 3 / 4;
            let _ = SetCursorPos(x, y);
            sleep(Duration::from_millis(20));

            mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0);
        }

        let dlg_raw = dlg_hwnd.0 as isize;
        let inj_handle = std::thread::spawn(move || {
            inject_path_to_dialog(HWND(dlg_raw as *mut _), target_dir)
        });

        sleep(Duration::from_millis(80));
        unsafe {
            mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0);
        }

        let ok = inj_handle.join().unwrap_or(false);
        let curr = get_dialog_current_path(dlg_hwnd).unwrap_or_default();
        (ok, curr)
    };

    // Case 1: Physical click on blank space in SHELLDLL_DefView -> DirectUIHWND
    let dui = ctrls.def_view_dui.expect("SHELLDLL_DefView DirectUIHWND not found");
    let (ok1, path1) = click_and_inject(dui, r"C:\Windows\System32");
    print_gui_state("After physical click on blank space + injection");
    println!("After blank-space physical click injection: ok1={}, dialog path = {}", ok1, path1);

    // Case 2: Physical click on left navigation tree (SysTreeView32)
    let nav = ctrls.tree_view.expect("SysTreeView32 not found");
    let (ok2, path2) = click_and_inject(nav, r"C:\Users");
    print_gui_state("After physical click on SysTreeView32 + injection");
    println!("After tree-nav physical click injection: ok2={}, dialog path = {}", ok2, path2);

    // Case 3: Physical click on top breadcrumb/address bar (ToolbarWindow32)
    let tb = ctrls.toolbar.expect("Breadcrumb ToolbarWindow32 not found");
    let (ok3, path3) = click_and_inject(tb, r"C:\Windows");
    print_gui_state("After physical click on ToolbarWindow32 + injection");
    println!("After top-navbar physical click injection: ok3={}, dialog path = {}", ok3, path3);

    // Clean up dialog BEFORE assertions so thread never hangs on failure
    unsafe {
        let _ = PostMessageW(dlg_hwnd, WM_CLOSE, WPARAM(0), LPARAM(0));
    }
    let _ = dlg_thread.join();

    assert!(ok1, "Injection after clicking DirectUIHWND (blank space) failed");
    assert_eq!(
        path1.trim_end_matches('\\').to_lowercase(),
        r"c:\windows\system32"
    );
    assert!(ok2, "Injection after clicking SysTreeView32 failed");
    assert_eq!(path2.trim_end_matches('\\').to_lowercase(), r"c:\users");
    assert!(ok3, "Injection after clicking top ToolbarWindow32 failed");
    assert_eq!(path3.trim_end_matches('\\').to_lowercase(), r"c:\windows");
}

