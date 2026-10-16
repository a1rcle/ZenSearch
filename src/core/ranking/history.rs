use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::utils::paths::get_appdata_dir;

#[derive(Debug, Clone, Default)]
pub struct LaunchRecord {
    pub count: u32,
    pub last_launch_secs: u64,
}

#[derive(Debug, Clone, Default)]
pub struct HistoryState {
    pub items: HashMap<String, LaunchRecord>,
    pub query_bindings: HashMap<String, String>,
}

pub struct HistoryManager {
    file_path: PathBuf,
    state: Mutex<HistoryState>,
}

static INSTANCE: OnceLock<Arc<HistoryManager>> = OnceLock::new();

impl HistoryManager {
    pub fn global() -> Arc<HistoryManager> {
        INSTANCE
            .get_or_init(|| {
                let dir = get_appdata_dir();
                let file_path = dir.join("history.tsv");
                let mgr = HistoryManager {
                    file_path,
                    state: Mutex::new(HistoryState::default()),
                };
                mgr.load();
                Arc::new(mgr)
            })
            .clone()
    }

    fn now_secs() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
    }

    pub fn load(&self) {
        if !self.file_path.exists() {
            return;
        }

        let content = match fs::read_to_string(&self.file_path) {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("Failed to read history file: {}", e);
                return;
            }
        };

        let mut items = HashMap::new();
        let mut query_bindings = HashMap::new();

        for line in content.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let parts: Vec<&str> = line.split('\t').collect();
            if parts.is_empty() {
                continue;
            }

            match parts[0] {
                "ITEM" if parts.len() >= 4 => {
                    let path = parts[1].to_string();
                    let count = parts[2].parse::<u32>().unwrap_or(0);
                    let last_secs = parts[3].parse::<u64>().unwrap_or(0);
                    items.insert(path, LaunchRecord { count, last_launch_secs: last_secs });
                }
                "BIND" if parts.len() >= 3 => {
                    let query = parts[1].to_lowercase();
                    let target_path = parts[2].to_string();
                    query_bindings.insert(query, target_path);
                }
                _ => {}
            }
        }

        if let Ok(mut state) = self.state.lock() {
            state.items = items;
            state.query_bindings = query_bindings;
        }
    }

    pub fn save(&self) {
        let state = match self.state.lock() {
            Ok(s) => s.clone(),
            Err(_) => return,
        };

        let mut out = String::with_capacity(4096);
        out.push_str("# ZenSearch History Store\n");

        for (query, target) in &state.query_bindings {
            out.push_str(&format!("BIND\t{}\t{}\n", query, target));
        }

        for (path, rec) in &state.items {
            out.push_str(&format!("ITEM\t{}\t{}\t{}\n", path, rec.count, rec.last_launch_secs));
        }

        if let Some(parent) = self.file_path.parent() {
            let _ = fs::create_dir_all(parent);
        }

        if let Err(e) = fs::write(&self.file_path, out) {
            tracing::warn!("Failed to persist history store: {}", e);
        }
    }

    pub fn record_launch(&self, query: &str, path: &str) {
        let now = Self::now_secs();
        let trimmed_query = query.trim().to_lowercase();

        {
            if let Ok(mut state) = self.state.lock() {
                let rec = state.items.entry(path.to_string()).or_default();
                rec.count = rec.count.saturating_add(1);
                rec.last_launch_secs = now;

                if !trimmed_query.is_empty() {
                    state.query_bindings.insert(trimmed_query, path.to_string());
                }
            }
        }

        self.save();
    }

    pub fn get_item_record(&self, path: &str) -> Option<LaunchRecord> {
        self.state.lock().ok()?.items.get(path).cloned()
    }

    pub fn get_binding(&self, query: &str) -> Option<String> {
        let trimmed = query.trim().to_lowercase();
        self.state.lock().ok()?.query_bindings.get(&trimmed).cloned()
    }
}
