use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::ERROR_SUCCESS;
use windows::Win32::System::Registry::{
    RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW, HKEY,
    HKEY_CURRENT_USER, KEY_READ, KEY_WRITE, REG_SZ,
};

const TASK_NAME: &str = "ZenSearch_Startup";
const RUN_SUBKEY: PCWSTR = w!(r"Software\Microsoft\Windows\CurrentVersion\Run");
const VALUE_NAME: PCWSTR = w!("ZenSearch");
const CREATE_NO_WINDOW: u32 = 0x08000000;

static TASK_SCHEDULER_CACHED_STATUS: AtomicBool = AtomicBool::new(false);
static TASK_SCHEDULER_LAST_CHECK_SECS: AtomicU64 = AtomicU64::new(0);
const CACHE_TTL_SECS: u64 = 60;

pub fn check_task_scheduler_startup() -> bool {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let last = TASK_SCHEDULER_LAST_CHECK_SECS.load(Ordering::Relaxed);
    if last > 0 && now.saturating_sub(last) < CACHE_TTL_SECS {
        return TASK_SCHEDULER_CACHED_STATUS.load(Ordering::Relaxed);
    }

    let mut cmd = Command::new("schtasks");
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let output = cmd
        .args(["/Query", "/TN", TASK_NAME])
        .output();

    let exists = if let Ok(out) = output {
        out.status.success()
    } else {
        false
    };

    TASK_SCHEDULER_CACHED_STATUS.store(exists, Ordering::Relaxed);
    TASK_SCHEDULER_LAST_CHECK_SECS.store(now, Ordering::Relaxed);
    exists
}

pub fn delete_task_scheduler_startup() -> Result<(), String> {
    let mut cmd = Command::new("schtasks");
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let output = cmd
        .args(["/Delete", "/F", "/TN", TASK_NAME])
        .output()
        .map_err(|e| e.to_string())?;

    if output.status.success() {
        TASK_SCHEDULER_CACHED_STATUS.store(false, Ordering::Relaxed);
        Ok(())
    } else {
        let err = String::from_utf8_lossy(&output.stderr);
        Err(err.trim().to_string())
    }
}

pub fn get_registry_startup_command() -> Option<String> {
    unsafe {
        let mut hkey = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, RUN_SUBKEY, 0, KEY_READ, &mut hkey) == ERROR_SUCCESS {
            let mut data_len = 0u32;
            let status = RegQueryValueExW(
                hkey,
                VALUE_NAME,
                None,
                None,
                None,
                Some(&mut data_len as *mut u32),
            );
            if status == ERROR_SUCCESS && data_len > 0 {
                let mut buf = vec![0u16; (data_len as usize / 2) + 1];
                let status = RegQueryValueExW(
                    hkey,
                    VALUE_NAME,
                    None,
                    None,
                    Some(buf.as_mut_ptr() as *mut u8),
                    Some(&mut data_len),
                );
                let _ = RegCloseKey(hkey);
                if status == ERROR_SUCCESS {
                    let text = String::from_utf16_lossy(&buf);
                    let trimmed = text.trim_matches(char::from(0)).trim().to_string();
                    return Some(trimmed);
                }
            } else {
                let _ = RegCloseKey(hkey);
            }
        }
    }
    None
}

fn is_valid_startup_command(cmd: &str, curr_exe_path: Option<&str>) -> bool {
    let trimmed = cmd.trim();
    if trimmed.is_empty() {
        return false;
    }

    let cmd_lower = trimmed.to_lowercase();

    // 排除已过期的旧版 Python 脚本残留
    if cmd_lower.contains(".py") || cmd_lower.contains("python") {
        return false;
    }

    // 检查是否包含当前正在运行的可执行文件路径
    if let Some(curr_path) = curr_exe_path {
        if cmd_lower.contains(&curr_path.to_lowercase()) {
            return true;
        }
    }

    // 提取命令中的 exe 路径并检查该文件在磁盘上是否存在
    let exe_path_str = if let Some(stripped) = trimmed.strip_prefix('"') {
        if let Some(end) = stripped.find('"') {
            &stripped[..end]
        } else {
            trimmed.split_whitespace().next().unwrap_or(trimmed)
        }
    } else {
        trimmed.split_whitespace().next().unwrap_or(trimmed)
    };

    let path = std::path::Path::new(exe_path_str);
    path.is_file() && exe_path_str.to_lowercase().ends_with("zensearch.exe")
}

pub fn check_registry_startup() -> bool {
    if let Some(cmd) = get_registry_startup_command() {
        let curr_exe = std::env::current_exe().ok();
        let curr_str = curr_exe.as_ref().map(|p| p.to_string_lossy());
        is_valid_startup_command(&cmd, curr_str.as_deref())
    } else {
        false
    }
}

pub fn is_autostart_enabled() -> bool {
    check_registry_startup() || check_task_scheduler_startup()
}

pub fn enable_registry_autostart() -> Result<(), String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let run_cmd = format!("\"{}\" --silent", exe.to_string_lossy());
    let wide_cmd: Vec<u16> = run_cmd.encode_utf16().chain(std::iter::once(0)).collect();

    unsafe {
        let mut hkey = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, RUN_SUBKEY, 0, KEY_WRITE, &mut hkey) == ERROR_SUCCESS {
            let status = RegSetValueExW(
                hkey,
                VALUE_NAME,
                0,
                REG_SZ,
                Some(std::slice::from_raw_parts(
                    wide_cmd.as_ptr() as *const u8,
                    wide_cmd.len() * 2,
                )),
            );
            let _ = RegCloseKey(hkey);
            if status == ERROR_SUCCESS {
                tracing::info!("Registered startup command in HKCU\\Run: {}", run_cmd);
                Ok(())
            } else {
                Err(format!("RegSetValueExW failed with code {}", status.0))
            }
        } else {
            Err("Failed to open HKCU Run registry key".into())
        }
    }
}

pub fn disable_registry_autostart() -> Result<(), String> {
    unsafe {
        let mut hkey = HKEY::default();
        if RegOpenKeyExW(HKEY_CURRENT_USER, RUN_SUBKEY, 0, KEY_WRITE, &mut hkey) == ERROR_SUCCESS {
            let status = RegDeleteValueW(hkey, VALUE_NAME);
            let _ = RegCloseKey(hkey);
            if status == ERROR_SUCCESS {
                tracing::info!("Removed ZenSearch startup entry from HKCU\\Run");
                Ok(())
            } else {
                Err(format!("RegDeleteValueW failed with code {}", status.0))
            }
        } else {
            Err("Failed to open HKCU Run registry key".into())
        }
    }
}

pub fn enable_autostart() -> Result<(), String> {
    enable_registry_autostart()
}

pub fn disable_autostart() -> Result<(), String> {
    let mut errors = Vec::new();

    if let Err(e) = disable_registry_autostart() {
        errors.push(format!("Registry: {}", e));
    }

    if check_task_scheduler_startup() {
        if let Err(e) = delete_task_scheduler_startup() {
            errors.push(format!("TaskScheduler: {}", e));
        } else {
            tracing::info!("Deleted ZenSearch_Startup scheduled task");
        }
    }

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_reject_legacy_python_autostart_command() {
        let legacy_cmd = r#""D:\Install_path\anaconda3\pythonw.exe" "D:\ZenSearch\main.py" --silent"#;
        assert!(!is_valid_startup_command(legacy_cmd, Some(r"D:\ZenSearch\target\release\zensearch.exe")));
    }

    #[test]
    fn test_accept_matching_exe_command() {
        let curr = r"D:\ZenSearch\target\release\zensearch.exe";
        let valid_cmd = format!("\"{}\" --silent", curr);
        assert!(is_valid_startup_command(&valid_cmd, Some(curr)));
    }

    #[test]
    fn test_registry_command_read_and_check() {
        if let Some(cmd) = get_registry_startup_command() {
            assert!(!cmd.is_empty());
            assert!(!cmd.contains(".py"), "Legacy python script must not be in registry command");
        }
    }
}
