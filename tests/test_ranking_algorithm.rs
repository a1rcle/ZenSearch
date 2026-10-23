use zensearch::core::everything::SearchResult;
use zensearch::core::ranking::{HistoryManager, Ranker, SearchContext};

#[test]
fn test_exact_match_beats_fuzzy_or_partial() {
    let history = HistoryManager::global();
    let candidates = vec![
        SearchResult {
            filename: "notepad_extra_long_tool.exe".to_string(),
            path: r"C:\Tools\notepad_extra_long_tool.exe".to_string(),
        },
        SearchResult {
            filename: "notepad.exe".to_string(),
            path: r"C:\Windows\System32\notepad.exe".to_string(),
        },
        SearchResult {
            filename: "my_notepad_notes.txt".to_string(),
            path: r"D:\docs\my_notepad_notes.txt".to_string(),
        },
    ];

    let ranked = Ranker::rank(candidates, "notepad", SearchContext::Launcher, &history);
    assert_eq!(ranked[0].filename, "notepad.exe");
}

#[test]
fn test_prefix_match_beats_suffix_or_contains() {
    let history = HistoryManager::global();
    let candidates = vec![
        SearchResult {
            filename: "my_calc_notes.txt".to_string(),
            path: r"C:\my_calc_notes.txt".to_string(),
        },
        SearchResult {
            filename: "calc.exe".to_string(),
            path: r"C:\Windows\System32\calc.exe".to_string(),
        },
    ];

    let ranked = Ranker::rank(candidates, "calc", SearchContext::Launcher, &history);
    assert_eq!(ranked[0].filename, "calc.exe");
}

#[test]
fn test_acronym_match() {
    let history = HistoryManager::global();
    let candidates = vec![
        SearchResult {
            filename: "vsc_other.txt".to_string(),
            path: r"C:\vsc_other.txt".to_string(),
        },
        SearchResult {
            filename: "Visual Studio Code.exe".to_string(),
            path: r"C:\Program Files\Visual Studio Code.exe".to_string(),
        },
    ];

    let ranked = Ranker::rank(candidates, "vsc", SearchContext::Launcher, &history);
    // Visual Studio Code should get high acronym match score
    assert_eq!(ranked[0].filename, "Visual Studio Code.exe");
}

#[test]
fn test_noisy_directories_are_penalized() {
    let history = HistoryManager::global();
    let candidates = vec![
        SearchResult {
            filename: "index.js".to_string(),
            path: r"D:\projects\web\node_modules\lodash\index.js".to_string(),
        },
        SearchResult {
            filename: "index.js".to_string(),
            path: r"D:\projects\web\src\index.js".to_string(),
        },
    ];

    let ranked = Ranker::rank(candidates, "index.js", SearchContext::Launcher, &history);
    assert_eq!(ranked[0].path, r"D:\projects\web\src\index.js");

    let recent_candidates = vec![
        SearchResult {
            filename: "report.docx".to_string(),
            path: r"C:\Users\User\AppData\Roaming\Microsoft\Windows\Recent\report.docx".to_string(),
        },
        SearchResult {
            filename: "report.docx".to_string(),
            path: r"D:\work\report.docx".to_string(),
        },
    ];
    let ranked_recent = Ranker::rank(recent_candidates, "report.docx", SearchContext::Launcher, &history);
    assert_eq!(ranked_recent[0].path, r"D:\work\report.docx");
}

#[test]
fn test_query_binding_boost() {
    let history = HistoryManager::global();
    let target_path = r"C:\CustomApps\SpecialTool.exe";
    history.record_launch("sp", target_path);

    let candidates = vec![
        SearchResult {
            filename: "spotify.exe".to_string(),
            path: r"C:\Spotify\spotify.exe".to_string(),
        },
        SearchResult {
            filename: "SpecialTool.exe".to_string(),
            path: target_path.to_string(),
        },
    ];

    let ranked = Ranker::rank(candidates, "sp", SearchContext::Launcher, &history);
    assert_eq!(ranked[0].path, target_path);
}
