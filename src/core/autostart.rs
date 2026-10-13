use std::process::Command;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::ERROR_SUCCESS;
use windows::Win32::System::Registry::{
    RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW, HKEY,
    HKEY_CURRENT_USER, KEY_READ, KEY_WRITE, REG_SZ,
};

const TASK_NAME: &str = "ZenSearch_Startup";
const RUN_SUBKEY: PCWSTR = w!(r"Software\Microsoft\Windows\CurrentVersion\Run");
const VALUE_NAME: PCWSTR = w!("ZenSearch");

pub fn check_task_scheduler_startup() -> bool {
    let output = Command::new("schtasks")
        .args(["/Query", "/TN", TASK_NAME])
        .output();

    if let Ok(out) = output {
        out.status.success()
    } else {
        false
    }
}

pub fn check_registry_startup() -> bool {
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
            let _ = RegCloseKey(hkey);
            status == ERROR_SUCCESS && data_len > 0
        } else {
            false
        }
    }
}

pub fn is_autostart_enabled() -> bool {
    check_task_scheduler_startup() || check_registry_startup()
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
                Ok(())
            } else {
                Err(format!("RegDeleteValueW failed with code {}", status.0))
            }
        } else {
            Err("Failed to open HKCU Run registry key".into())
        }
    }
}
