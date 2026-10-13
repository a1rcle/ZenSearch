use zensearch::core::scale_manager;
use windows::Win32::Foundation::RECT;

#[test]
fn test_high_dpi_context_and_metrics() {
    scale_manager::init_high_dpi_context();
    let (h_monitor, work) = scale_manager::get_focus_monitor_and_work_area();
    let scale = scale_manager::get_monitor_scale_factor(h_monitor);

    assert!(scale >= 1.0, "Scale factor should be at least 1.0, got {}", scale);
    assert!(work.right > work.left, "Work area width must be positive");
    assert!(work.bottom > work.top, "Work area height must be positive");

    // Collapsed launcher geometry check
    let collapsed = scale_manager::calculate_launcher_geometry(false, 0);
    let expected_min_phys_h = (60.0 * scale).round() as i32;
    assert!(
        collapsed.height >= expected_min_phys_h,
        "Collapsed height {} must be >= min required physical height {} (scale {})",
        collapsed.height,
        expected_min_phys_h,
        scale
    );

    // Expanded launcher geometry check
    let expanded = scale_manager::calculate_launcher_geometry(true, 5);
    assert!(
        expanded.height > collapsed.height,
        "Expanded height {} must be greater than collapsed height {}",
        expanded.height,
        collapsed.height
    );

    // Companion dock geometry check
    let dummy_exp = RECT {
        left: 100,
        top: 100,
        right: 1100,
        bottom: 800,
    };
    let companion = scale_manager::calculate_companion_dock_geometry(dummy_exp, false, 0);
    assert!(companion.width > 0);
    assert!(companion.height >= (44.0 * scale).round() as i32);
}

#[test]
fn test_companion_bottom_anchor_and_upward_growth() {
    scale_manager::init_high_dpi_context();
    let dummy_exp = RECT {
        left: 200,
        top: 200,
        right: 1200,
        bottom: 900,
    };

    let collapsed = scale_manager::calculate_companion_dock_geometry(dummy_exp, false, 0);
    let expanded = scale_manager::calculate_companion_dock_geometry(dummy_exp, true, 5);

    // Height increases
    assert!(expanded.height > collapsed.height);

    // Top moves upward (lower y coordinate) to accommodate results above input
    assert!(expanded.y < collapsed.y);

    // Bottom edge remains stationary at the exact anchor line
    let collapsed_bottom = collapsed.y + collapsed.height;
    let expanded_bottom = expanded.y + expanded.height;
    assert_eq!(
        collapsed_bottom, expanded_bottom,
        "Companion bottom edge must remain stationary when results expand upwards"
    );
}

#[test]
fn test_toolwindow_style_calculation() {
    use windows::Win32::UI::WindowsAndMessaging::{WS_EX_APPWINDOW, WS_EX_TOOLWINDOW};

    let initial_style = WS_EX_APPWINDOW.0;
    let modified = (initial_style | WS_EX_TOOLWINDOW.0) & !WS_EX_APPWINDOW.0;

    assert_eq!(modified & WS_EX_TOOLWINDOW.0, WS_EX_TOOLWINDOW.0, "Must contain WS_EX_TOOLWINDOW");
    assert_eq!(modified & WS_EX_APPWINDOW.0, 0, "Must NOT contain WS_EX_APPWINDOW");
}
