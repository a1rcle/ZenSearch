use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use super::history::HistoryManager;
use crate::core::everything::SearchResult;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SearchContext {
    Launcher,
    Companion,
}

#[derive(Debug, Clone)]
pub struct ScoredResult {
    pub result: SearchResult,
    pub score: i64,
}

pub struct Ranker;

impl Ranker {
    pub fn rank(
        candidates: Vec<SearchResult>,
        query: &str,
        context: SearchContext,
        history: &HistoryManager,
    ) -> Vec<SearchResult> {
        let trimmed_query = query.trim().to_lowercase();
        if trimmed_query.is_empty() {
            return candidates;
        }

        let now_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);

        let bound_path = history.get_binding(&trimmed_query);

        let mut scored_list: Vec<ScoredResult> = candidates
            .into_iter()
            .map(|res| {
                let score = Self::calculate_score(&res, &trimmed_query, context, now_secs, bound_path.as_deref(), history);
                ScoredResult { result: res, score }
            })
            .collect();

        // Sort descending by score. If score is equal, shorter filename first, then shorter path
        scored_list.sort_by(|a, b| {
            b.score
                .cmp(&a.score)
                .then_with(|| a.result.filename.len().cmp(&b.result.filename.len()))
                .then_with(|| a.result.path.len().cmp(&b.result.path.len()))
        });

        scored_list.into_iter().map(|s| s.result).collect()
    }

    pub fn calculate_score(
        res: &SearchResult,
        query_lower: &str,
        context: SearchContext,
        now_secs: u64,
        bound_path: Option<&str>,
        history: &HistoryManager,
    ) -> i64 {
        let mut score: i64 = 0;
        let filename_lower = res.filename.to_lowercase();
        let path_lower = res.path.to_lowercase();
        let ext = Path::new(&res.filename)
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_lowercase();

        // -------------------------------------------------------------
        // 1. Text Matching Quality (S_match)
        // -------------------------------------------------------------
        let stem = Path::new(&filename_lower)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or(&filename_lower);

        if filename_lower == query_lower || stem == query_lower {
            // Exact full match
            score += 12_000;
        } else if filename_lower.starts_with(query_lower) {
            // Filename prefix match
            score += 6_000;
        } else if stem.starts_with(query_lower) {
            // Stem prefix match
            score += 5_500;
        } else if let Some(idx) = filename_lower.find(query_lower) {
            // Substring in filename
            // Earlier index gets higher bonus
            let pos_bonus = 3_000i64.saturating_sub((idx as i64) * 30);
            score += pos_bonus.max(1_000);
        } else if Self::is_acronym_match(&res.filename, query_lower) {
            // CamelCase / Acronym match: e.g. "vsc" for "Visual Studio Code"
            score += 4_500;
        } else if path_lower.contains(query_lower) {
            // Matched in parent directory path only
            score += 300;
        } else {
            // Fallback fuzzy
            score += 100;
        }

        // Penalty for filename length compared to query (tighter match ranks higher)
        let len_diff = res.filename.len().saturating_sub(query_lower.len());
        score -= (len_diff as i64 * 3).min(300);

        // -------------------------------------------------------------
        // 2. Adaptive Learning & History Habits (S_history)
        // -------------------------------------------------------------
        // A. Specific Query Binding: User typed this query and previously opened this item
        if let Some(target) = bound_path {
            if target.eq_ignore_ascii_case(&res.path) {
                score += 15_000;
            }
        }

        // B. General Launch Frequency & Recency
        if let Some(record) = history.get_item_record(&res.path) {
            // Frequency boost: logarithmic scaling
            let freq_boost = ((record.count as f64 + 1.0).log2() * 600.0) as i64;
            score += freq_boost;

            // Recency boost: exponential decay with half-life ~ 3 days (259200 secs)
            if record.last_launch_secs > 0 && now_secs >= record.last_launch_secs {
                let diff_secs = (now_secs - record.last_launch_secs) as f64;
                let half_life = 259_200.0;
                let decay = (-diff_secs / half_life).exp2();
                let recency_boost = (decay * 3_000.0) as i64;
                score += recency_boost;
            }
        }

        // -------------------------------------------------------------
        // 3. File Type & Contextual Relevance (S_type)
        // -------------------------------------------------------------
        let is_executable = matches!(ext.as_str(), "exe" | "bat" | "cmd" | "appref-ms");
        let is_dir = Path::new(&res.path).is_dir();

        match context {
            SearchContext::Launcher => {
                // In general launcher, prioritize executable apps & tools
                if is_executable {
                    score += 2_000;
                }
            }
            SearchContext::Companion => {
                // In companion dock (Explorer or File Dialog), folders are crucial
                if is_dir {
                    score += 2_500;
                }
            }
        }

        // -------------------------------------------------------------
        // 4. Path Depth & Noisy Directory Penalty (S_path)
        // -------------------------------------------------------------
        // Depth penalty: every backslash adds a penalty
        let slash_count = res.path.chars().filter(|&c| c == '\\' || c == '/').count();
        score -= (slash_count as i64 * 40).min(500);

        // Heavy penalty for internal caches, build artifacts, and vendor files
        if path_lower.contains(r"\node_modules\")
            || path_lower.contains(r"\.git\")
            || path_lower.contains(r"\target\debug\")
            || path_lower.contains(r"\target\release\")
            || path_lower.contains(r"\appdata\local\temp\")
            || path_lower.contains(r"\$recycle.bin\")
            || path_lower.contains(r"\appdata\roaming\microsoft\windows\recent\")
            || path_lower.contains(r"\appdata\roaming\microsoft\office\recent\")
        {
            score -= 6_000;
        }

        score
    }

    /// Check if query matches word initials / CamelCase
    /// e.g. "vsc" matches "Visual Studio Code" or "VisualStudioCode"
    fn is_acronym_match(name: &str, query_lower: &str) -> bool {
        let mut initials = String::with_capacity(8);
        let mut prev_is_sep = true;

        for c in name.chars() {
            if c.is_alphanumeric() {
                if prev_is_sep || c.is_uppercase() {
                    initials.push(c.to_ascii_lowercase());
                }
                prev_is_sep = false;
            } else {
                prev_is_sep = true;
            }
        }

        initials.contains(query_lower)
    }
}
