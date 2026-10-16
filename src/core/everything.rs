use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread::sleep;
use std::time::Duration;
use windows::core::{w, PCWSTR};

use windows::Win32::System::Registry::{
    RegCloseKey, RegOpenKeyExW, RegQueryValueExW, HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE,
    KEY_READ,
};
use windows::Win32::UI::WindowsAndMessaging::FindWindowW;

// Link Everything64.dll C export symbols
#[link(name = "Everything64")]
extern "system" {
    fn Everything_SetSearchW(lpString: *const u16);
    fn Everything_SetRequestFlags(dwRequestFlags: u32);
    fn Everything_SetMax(dwMax: u32);
    fn Everything_SetSort(dwSortType: u32);
    fn Everything_SetMatchPath(bEnable: i32);
    fn Everything_SetMatchCase(bEnable: i32);
    fn Everything_SetMatchWholeWord(bEnable: i32);
    fn Everything_SetRegex(bEnable: i32);
    fn Everything_QueryW(bWait: i32) -> i32;
    fn Everything_GetNumResults() -> u32;
    fn Everything_GetTotResults() -> u32;
    fn Everything_GetResultFullPathNameW(nIndex: u32, lpString: *mut u16, nMaxCount: u32) -> u32;
    fn Everything_GetLastError() -> u32;
}

pub const EVERYTHING_SORT_NAME_ASCENDING: u32 = 1;
pub const EVERYTHING_SORT_PATH_ASCENDING: u32 = 3;
pub const EVERYTHING_SORT_DATE_MODIFIED_DESCENDING: u32 = 14;
pub const EVERYTHING_SORT_RUN_COUNT_DESCENDING: u32 = 20;

#[derive(Debug, Clone)]
pub struct SearchResult {
    pub path: String,
    pub filename: String,
}

#[derive(Debug, Clone, Default)]
pub struct QueryOutput {
    pub results: Vec<SearchResult>,
    pub total_matched: u32,
}

impl std::ops::Deref for QueryOutput {
    type Target = [SearchResult];
    fn deref(&self) -> &Self::Target {
        &self.results
    }
}

pub fn is_everything_running() -> bool {
    unsafe {
        if let Ok(hwnd) = FindWindowW(w!("EVERYTHING_TASKBAR_NOTIFICATION"), PCWSTR::null()) {
            if hwnd.0 != std::ptr::null_mut() {
                return true;
            }
        }
        if let Ok(hwnd) = FindWindowW(w!("EVERYTHING_TASKBAR_NOTIFICATION_1.5"), PCWSTR::null()) {
            if hwnd.0 != std::ptr::null_mut() {
                return true;
            }
        }
        // Fallback: check Everything SDK IPC status
        // EVERYTHING_ERROR_IPC is 2
        let _ = Everything_QueryW(0);
        let err = Everything_GetLastError();
        err != 2
    }
}

pub fn probe_everything_executable() -> Option<PathBuf> {
    // Level 2: Registry Probe
    let reg_paths = [
        (HKEY_CURRENT_USER, r"Software\voidtools\Everything"),
        (HKEY_LOCAL_MACHINE, r"Software\voidtools\Everything"),
        (HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Everything.exe"),
    ];

    for (root, subkey) in reg_paths {
        let wide_subkey: Vec<u16> = OsStr::new(subkey).encode_wide().chain(std::iter::once(0)).collect();
        let mut hkey = HKEY::default();
        unsafe {
            if RegOpenKeyExW(root, PCWSTR(wide_subkey.as_ptr()), 0, KEY_READ, &mut hkey).is_ok() {
                let mut buf = [0u16; 512];
                let mut len = (buf.len() * 2) as u32;
                let val_name = w!("InstallPath");
                let res = RegQueryValueExW(hkey, val_name, None, None, Some(buf.as_mut_ptr() as *mut u8), Some(&mut len));
                let _ = RegCloseKey(hkey);
                if res.is_ok() && len > 0 {
                    let dir_str = String::from_utf16_lossy(&buf[..(len as usize / 2).saturating_sub(1)]);
                    let exe_path = Path::new(&dir_str).join("Everything.exe");
                    if exe_path.exists() {
                        return Some(exe_path);
                    }
                }
            }
        }
    }

    // Level 3: PATH environment probe
    if let Ok(paths) = std::env::var("PATH") {
        for p in std::env::split_paths(&paths) {
            let candidate = p.join("Everything.exe");
            if candidate.exists() {
                return Some(candidate);
            }
        }
    }

    // Level 3.5: Check typical installation paths before bundled fallback
    for candidate in [
        r"C:\Program Files\Everything\Everything.exe",
        r"C:\Program Files (x86)\Everything\Everything.exe",
        r"D:\Install_path\Everything\Everything.exe",
    ] {
        let p = PathBuf::from(candidate);
        if p.exists() {
            return Some(p);
        }
    }

    // Level 4: Bundled tools probe
    let bundled = crate::utils::paths::get_bundled_tools_dir().join("Everything.exe");
    if bundled.exists() {
        return Some(bundled);
    }

    None
}

pub fn ensure_everything_service() -> bool {
    // Level 1: already running
    if is_everything_running() {
        tracing::debug!("Everything IPC service is active");
        return true;
    }

    tracing::warn!("Everything is not running, initiating auto-launch probe...");
    if let Some(exe_path) = probe_everything_executable() {
        tracing::info!("Launching Everything background daemon from: {:?}", exe_path);
        let spawn_res = Command::new(&exe_path)
            .arg("-startup")
            .spawn();

        if let Ok(_child) = spawn_res {
            // Poll for IPC readiness up to 3 seconds
            for _ in 0..30 {
                sleep(Duration::from_millis(100));
                if is_everything_running() {
                    tracing::info!("Everything service successfully connected");
                    return true;
                }
            }
            tracing::warn!("Everything spawned but IPC window was not detected within timeout");
        } else {
            tracing::error!("Failed to launch Everything process");
        }
    } else {
        tracing::error!("No Everything.exe binary found in system or bundled tools");
    }

    false
}

pub fn execute_query(query: &str, max_results: u32) -> QueryOutput {
    let trimmed = query.trim();
    if trimmed.is_empty() {
        return QueryOutput::default();
    }

    let wide_query: Vec<u16> = OsStr::new(trimmed)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    let enable_match_path = if trimmed.contains(' ') || trimmed.contains('\\') || trimmed.contains('/') {
        1
    } else {
        0
    };

    unsafe {
        // Reset query flags to prevent state pollution
        Everything_SetMatchCase(0);
        Everything_SetMatchWholeWord(0);
        Everything_SetRegex(0);
        Everything_SetMatchPath(enable_match_path);

        Everything_SetSearchW(wide_query.as_ptr());
        Everything_SetRequestFlags(0x00000001 | 0x00000002); // EVERYTHING_REQUEST_FILE_NAME | EVERYTHING_REQUEST_PATH
        Everything_SetMax(max_results);
        Everything_SetSort(EVERYTHING_SORT_DATE_MODIFIED_DESCENDING); // 14: Date Modified Descending

        if Everything_QueryW(1) != 0 {
            let count = Everything_GetNumResults();
            let total_matched = Everything_GetTotResults();
            let mut results = Vec::with_capacity(count as usize);
            let mut buffer = [0u16; 1024];

            for i in 0..count {
                let len = Everything_GetResultFullPathNameW(i, buffer.as_mut_ptr(), 1024);
                if len > 0 {
                    let path = String::from_utf16_lossy(&buffer[..len as usize]);
                    let filename = Path::new(&path)
                        .file_name()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_else(|| path.clone());
                    results.push(SearchResult { path, filename });
                }
            }
            return QueryOutput { results, total_matched };
        } else {
            let err = Everything_GetLastError();
            if err != 0 {
                tracing::warn!("Everything_QueryW failed with error code: {}", err);
            }
        }
    }

    QueryOutput::default()
}

pub fn execute_ranked_query(
    query: &str,
    max_results: u32,
    context: crate::core::ranking::SearchContext,
) -> QueryOutput {
    let trimmed = query.trim();
    if trimmed.is_empty() {
        return QueryOutput::default();
    }

    // Two-stage ranking: fetch a broader candidate pool (up to 200 items)
    let candidate_pool_size = (max_results * 10).max(100).min(300);
    let raw_output = execute_query(query, candidate_pool_size);

    if raw_output.results.is_empty() {
        return raw_output;
    }

    // Rank candidates using ZenSearch ranking algorithm
    let history = crate::core::ranking::HistoryManager::global();
    let ranked = crate::core::ranking::Ranker::rank(
        raw_output.results,
        trimmed,
        context,
        &history,
    );

    // Truncate to desired max_results
    let final_results: Vec<SearchResult> = ranked.into_iter().take(max_results as usize).collect();

    QueryOutput {
        results: final_results,
        total_matched: raw_output.total_matched,
    }
}

