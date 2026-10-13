use std::path::PathBuf;

pub fn get_appdata_dir() -> PathBuf {
    if let Some(mut dir) = dirs::config_dir() {
        dir.push("ZenSearch");
        let _ = std::fs::create_dir_all(&dir);
        return dir;
    }
    PathBuf::from(r"C:\ProgramData\ZenSearch")
}

pub fn get_log_dir() -> PathBuf {
    if let Some(mut dir) = dirs::data_local_dir() {
        dir.push("ZenSearch");
        dir.push("logs");
        let _ = std::fs::create_dir_all(&dir);
        return dir;
    }
    let mut fallback = get_appdata_dir();
    fallback.push("logs");
    let _ = std::fs::create_dir_all(&fallback);
    fallback
}

pub fn get_exe_dir() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            return parent.to_path_buf();
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

pub fn get_bundled_tools_dir() -> PathBuf {
    let exe_dir = get_exe_dir();
    let tools = exe_dir.join("tools");
    if tools.exists() {
        return tools;
    }
    // Fallback if running from cargo workspace root
    let root_tools = PathBuf::from("tools");
    if root_tools.exists() {
        return root_tools;
    }
    tools
}
