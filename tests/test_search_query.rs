use zensearch::core::everything::{execute_query, QueryOutput, SearchResult};

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
