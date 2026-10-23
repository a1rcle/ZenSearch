use zensearch::core::everything::{
    build_effective_query, execute_query, is_excluded, QueryOutput, SearchResult,
};

#[test]
fn test_lnk_and_recent_exclusion() {
    assert!(is_excluded(r"C:\Users\User\Desktop\test.lnk", "test.lnk"));
    assert!(is_excluded(
        r"C:\Users\User\AppData\Roaming\Microsoft\Windows\Recent\doc.docx.lnk",
        "doc.docx.lnk"
    ));
    assert!(is_excluded(
        r"C:\Users\User\AppData\Roaming\Microsoft\Windows\Recent\CustomItem",
        "CustomItem"
    ));
    assert!(is_excluded(
        r"C:\Users\User\AppData\Roaming\Microsoft\Office\Recent\file.docx",
        "file.docx"
    ));

    // 正常文件不受影响
    assert!(!is_excluded(r"D:\ZenSearch\README.md", "README.md"));
    assert!(!is_excluded(r"C:\Program Files\App\app.exe", "app.exe"));
}

#[test]
fn test_build_effective_query() {
    let q = build_effective_query("test");
    assert_eq!(
        q,
        r#"test !*.lnk !"*\AppData\Roaming\Microsoft\Windows\Recent\*""#
    );
    assert_eq!(build_effective_query(""), "");
    assert_eq!(build_effective_query("   "), "");
}

#[test]
fn test_query_output_semantics() {
    let empty_output = execute_query("", 100);
    assert_eq!(empty_output.len(), 0);
    assert_eq!(empty_output.total_matched, 0);

    let whitespace_output = execute_query("   ", 100);
    assert_eq!(whitespace_output.len(), 0);
    assert_eq!(whitespace_output.total_matched, 0);

    // Verify Deref implementation allows slice methods
    let dummy_output = QueryOutput {
        results: vec![
            SearchResult {
                path: r"C:\Windows\explorer.exe".to_string(),
                filename: "explorer.exe".to_string(),
            },
            SearchResult {
                path: r"C:\Windows\notepad.exe".to_string(),
                filename: "notepad.exe".to_string(),
            },
        ],
        total_matched: 42,
    };

    assert_eq!(dummy_output.len(), 2);
    assert!(!dummy_output.is_empty());
    assert_eq!(dummy_output[0].filename, "explorer.exe");
    assert_eq!(dummy_output[1].filename, "notepad.exe");
    assert_eq!(dummy_output.total_matched, 42);

    let filenames: Vec<&str> = dummy_output.iter().map(|s| s.filename.as_str()).collect();
    assert_eq!(filenames, vec!["explorer.exe", "notepad.exe"]);
}
