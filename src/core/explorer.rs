use std::path::PathBuf;
use windows::core::{Interface, VARIANT};
use windows::Win32::Foundation::HWND;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_LOCAL_SERVER,
    COINIT_APARTMENTTHREADED,
};
use windows::Win32::UI::Shell::{IShellWindows, ShellWindows};
use windows::Win32::UI::WindowsAndMessaging::{GetClassNameW, GetParent};

pub struct ComGuard {
    should_uninit: bool,
}

impl ComGuard {
    pub fn new() -> Self {
        let should_uninit = unsafe {
            CoInitializeEx(None, COINIT_APARTMENTTHREADED).is_ok()
        };
        ComGuard { should_uninit }
    }
}

impl Drop for ComGuard {
    fn drop(&mut self) {
        if self.should_uninit {
            unsafe {
                CoUninitialize();
            }
        }
    }
}

pub fn is_explorer_window(hwnd: HWND) -> bool {
    let mut buf = [0u16; 64];
    let len = unsafe { GetClassNameW(hwnd, &mut buf) };
    if len > 0 {
        let name = String::from_utf16_lossy(&buf[..len as usize]);
        return name == "CabinetWClass" || name == "ExploreWClass";
    }
    false
}

fn file_url_to_path(url: &str) -> Option<String> {
    if !url.starts_with("file:///") {
        return None;
    }
    let trimmed = &url["file:///".len()..];
    let with_backslashes = trimmed.replace('/', "\\");
    let decoded = urlencoding_decode(&with_backslashes);
    if decoded.is_empty() {
        None
    } else {
        Some(decoded)
    }
}

fn urlencoding_decode(s: &str) -> String {
    let mut bytes = Vec::new();
    let mut chars = s.as_bytes().iter().copied();
    while let Some(b) = chars.next() {
        if b == b'%' {
            let h1 = chars.next();
            let h2 = chars.next();
            if let (Some(c1), Some(c2)) = (h1, h2) {
                let hex_str = [c1, c2];
                if let Ok(val) = u8::from_str_radix(std::str::from_utf8(&hex_str).unwrap_or(""), 16) {
                    bytes.push(val);
                    continue;
                }
            }
        }
        bytes.push(b);
    }
    String::from_utf8_lossy(&bytes).to_string()
}

pub fn get_explorer_path(target_hwnd: HWND) -> Option<String> {
    let _guard = ComGuard::new();

    unsafe {
        let shell_windows: IShellWindows =
            CoCreateInstance(&ShellWindows, None, CLSCTX_LOCAL_SERVER).ok()?;
        let count = shell_windows.Count().ok()?;

        for i in 0..count {
            let var_idx = VARIANT::from(i);

            if let Ok(dispatch) = shell_windows.Item(&var_idx) {
                if let Ok(web_browser) = dispatch.cast::<windows::Win32::UI::Shell::IWebBrowser2>() {
                    if let Ok(hwnd_val) = web_browser.HWND() {
                        let win_hwnd = HWND(hwnd_val.0 as _);
                        if win_hwnd == target_hwnd || is_parent_or_equal(win_hwnd, target_hwnd) {
                            if let Ok(bstr_url) = web_browser.LocationURL() {
                                let url_str = bstr_url.to_string();
                                if let Some(path) = file_url_to_path(&url_str) {
                                    if PathBuf::from(&path).exists() {
                                        return Some(path);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    None
}

fn is_parent_or_equal(parent: HWND, child: HWND) -> bool {
    if parent == child {
        return true;
    }
    let mut cur = child;
    while cur.0 != std::ptr::null_mut() {
        if let Ok(next) = unsafe { GetParent(cur) } {
            if next.0 == std::ptr::null_mut() {
                break;
            }
            if next == parent {
                return true;
            }
            cur = next;
        } else {
            break;
        }
    }
    false
}

/// Normalizes path and builds the explorer command line argument to open in a new window with selection.
pub fn build_explorer_select_args(path: &str) -> Option<String> {
    let raw = path.trim();
    if raw.is_empty() {
        return None;
    }

    // 1. Normalize path: convert forward slashes to backslashes
    let normalized = raw.replace('/', "\\");

    // 2. Drive root boundary check: C: or C:\ has no parent to select from, open root directly in new window
    let trimmed = normalized.trim_end_matches('\\');
    if trimmed.len() <= 2 && trimmed.ends_with(':') {
        return Some(format!("/n,\"{}:\\\"", &trimmed[0..1]));
    }

    // 3. For all other files and directories: trim trailing slashes to avoid escaping closing quote
    let clean_path = trimmed;
    Some(format!("/n, /select,\"{}\"", clean_path))
}

/// Opens a new Explorer window locating and selecting the target item (file or folder).
/// Forces a new independent window (/n) and never reuses existing windows/tabs.
pub fn open_in_new_explorer_and_select(path: &str) {
    if let Some(raw_args) = build_explorer_select_args(path) {
        use std::os::windows::process::CommandExt;
        let _ = std::process::Command::new("explorer")
            .raw_arg(raw_args)
            .spawn();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_explorer_select_args_file() {
        let args = build_explorer_select_args(r"D:\ZenSearch\Cargo.toml").unwrap();
        assert_eq!(args, r#"/n, /select,"D:\ZenSearch\Cargo.toml""#);
    }

    #[test]
    fn test_build_explorer_select_args_dir() {
        // Directory with trailing slash should have it trimmed
        let args = build_explorer_select_args(r"D:\ZenSearch\src\").unwrap();
        assert_eq!(args, r#"/n, /select,"D:\ZenSearch\src""#);
    }

    #[test]
    fn test_build_explorer_select_args_forward_slashes() {
        let args = build_explorer_select_args("D:/ZenSearch/src/").unwrap();
        assert_eq!(args, r#"/n, /select,"D:\ZenSearch\src""#);
    }

    #[test]
    fn test_build_explorer_select_args_drive_root() {
        let args1 = build_explorer_select_args(r"C:\").unwrap();
        assert_eq!(args1, r#"/n,"C:\""#);

        let args2 = build_explorer_select_args("D:").unwrap();
        assert_eq!(args2, r#"/n,"D:\""#);
    }

    #[test]
    fn test_build_explorer_select_args_empty() {
        assert!(build_explorer_select_args("   ").is_none());
    }
}
