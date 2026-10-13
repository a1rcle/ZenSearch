use windows::Win32::Foundation::{POINT, RECT};
use windows::Win32::Graphics::Gdi::{
    GetMonitorInfoW, MonitorFromPoint, MonitorFromRect, HMONITOR, MONITORINFO, MONITOR_DEFAULTTONEAREST,
};
use windows::Win32::UI::HiDpi::{
    GetDpiForMonitor, GetDpiForSystem, MDT_EFFECTIVE_DPI,
    SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};
use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;

#[derive(Debug, Clone, Copy)]
pub struct ScreenMetrics {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

pub fn init_high_dpi_context() {
    unsafe {
        if SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).is_err() {
            tracing::warn!("Per-Monitor V2 DPI awareness context failed to set; system fallback in effect");
        } else {
            tracing::info!("Per-Monitor V2 DPI awareness initialized");
        }
    }
}

pub fn get_focus_monitor_and_work_area() -> (HMONITOR, RECT) {
    unsafe {
        let mut pt = POINT::default();
        let _ = GetCursorPos(&mut pt);

        let h_monitor: HMONITOR = MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST);
        let mut mi = MONITORINFO {
            cbSize: std::mem::size_of::<MONITORINFO>() as u32,
            ..Default::default()
        };

        let work = if GetMonitorInfoW(h_monitor, &mut mi).as_bool() {
            mi.rcWork
        } else {
            RECT {
                left: 0,
                top: 0,
                right: 1920,
                bottom: 1080,
            }
        };

        (h_monitor, work)
    }
}

pub fn get_monitor_scale_factor(h_monitor: HMONITOR) -> f32 {
    unsafe {
        let mut dpi_x: u32 = 96;
        let mut dpi_y: u32 = 96;
        if GetDpiForMonitor(h_monitor, MDT_EFFECTIVE_DPI, &mut dpi_x, &mut dpi_y).is_ok() && dpi_x > 0 {
            dpi_x as f32 / 96.0
        } else {
            let sys_dpi = GetDpiForSystem();
            if sys_dpi > 0 {
                sys_dpi as f32 / 96.0
            } else {
                1.0
            }
        }
    }
}

pub fn get_focus_screen_work_area() -> RECT {
    get_focus_monitor_and_work_area().1
}

pub fn calculate_launcher_geometry(is_expanded: bool, result_count: usize) -> ScreenMetrics {
    let (h_monitor, work) = get_focus_monitor_and_work_area();
    let scale = get_monitor_scale_factor(h_monitor);

    let avail_w = work.right - work.left;
    let avail_h = work.bottom - work.top;

    // Logical dimensions in Slint DIP
    let avail_w_logical = avail_w as f32 / scale;
    let width_logical = (avail_w_logical * 0.38).clamp(560.0, 920.0);

    let base_height_logical: f32 = 64.0;
    let item_height_logical: f32 = 52.0;
    let list_height_logical: f32 = if is_expanded {
        ((result_count as f32) * item_height_logical).min(400.0) + 12.0
    } else {
        0.0
    };
    let height_logical = base_height_logical + list_height_logical;

    // Convert to physical pixels for window geometry
    let phys_w = (width_logical * scale).round() as i32;
    let phys_h = (height_logical * scale).round() as i32;

    let x = work.left + (avail_w - phys_w) / 2;
    let y = work.top + (avail_h as f32 * 0.22) as i32;

    ScreenMetrics {
        x,
        y,
        width: phys_w,
        height: phys_h,
    }
}

pub fn calculate_companion_dock_geometry(
    exp_rect: RECT,
    is_expanded: bool,
    result_count: usize,
) -> ScreenMetrics {
    let h_monitor = unsafe { MonitorFromRect(&exp_rect, MONITOR_DEFAULTTONEAREST) };
    let mut mi = MONITORINFO {
        cbSize: std::mem::size_of::<MONITORINFO>() as u32,
        ..Default::default()
    };
    let work = unsafe {
        if GetMonitorInfoW(h_monitor, &mut mi).as_bool() {
            mi.rcWork
        } else {
            get_focus_screen_work_area()
        }
    };
    let scale = get_monitor_scale_factor(h_monitor);

    let exp_w = exp_rect.right - exp_rect.left;
    let exp_w_logical = exp_w as f32 / scale;
    let comp_w_logical = (exp_w_logical * 0.52).clamp(400.0, 640.0);

    let base_height_logical: f32 = 46.0;
    let item_height_logical: f32 = 44.0;
    let list_height_logical: f32 = if is_expanded {
        ((result_count as f32) * item_height_logical).min(280.0) + 8.0
    } else {
        0.0
    };
    let comp_h_logical = base_height_logical + list_height_logical;

    let comp_w = (comp_w_logical * scale).round() as i32;
    let comp_h = (comp_h_logical * scale).round() as i32;
    let margin = (12.0 * scale).round() as i32;

    let target_x = exp_rect.right - comp_w - margin;
    let target_y = exp_rect.bottom - comp_h - margin;

    let safe_x = target_x.clamp(
        work.left + margin,
        (work.right - comp_w - margin).max(work.left),
    );
    let safe_y = target_y.clamp(
        work.top + margin,
        (work.bottom - comp_h - margin).max(work.top),
    );

    ScreenMetrics {
        x: safe_x,
        y: safe_y,
        width: comp_w,
        height: comp_h,
    }
}
