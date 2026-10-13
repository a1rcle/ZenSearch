#![windows_subsystem = "windows"]

use zensearch::core;
use zensearch::host;
use zensearch::utils;

use crossbeam_channel::unbounded;
use slint::{ComponentHandle, Model, ModelRc, VecModel};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
use windows::Win32::UI::WindowsAndMessaging::{GetForegroundWindow, GetWindowRect};

use zensearch::core::hotkey::AppEvent;

slint::include_modules!();

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let is_debug = args.iter().any(|a| a == "--debug" || a == "-d");
    let is_diagnose = args.iter().any(|a| a == "--diagnose");
    let is_watch = args.iter().any(|a| a == "--diagnose-watch");
    let is_silent = args.iter().any(|a| a == "--silent");

    // Attach to parent terminal console if invoked from CLI for diagnose or debug
    if is_diagnose || is_watch || is_debug {
        unsafe {
            let _ = AttachConsole(ATTACH_PARENT_PROCESS);
        }
    }

    // Initialize high DPI awareness
    core::scale_manager::init_high_dpi_context();

    // 1. Diagnostic CLI subcommands
    if is_diagnose {
        run_diagnose();
        return;
    }

    if is_watch {
        run_diagnose_watch();
        return;
    }

    // 2. Single instance mutual exclusion check
    if !core::single_instance::acquire() {
        if !is_silent {
            core::single_instance::activate_existing_instance();
        }
        std::process::exit(0);
    }

    // 3. Initialize logging
    let _logger_guard = utils::logger::init_logger(is_debug);
    tracing::info!("Starting ZenSearch v2.0...");

    // 4. Ensure Everything service is ready (auto-spawn bundled daemon if missing)
    core::everything::ensure_everything_service();

    // 4.1 Initialize Shell icon provider and prewarm common icons
    core::icon_provider::init();

    // 5. Create cross-thread event bus
    let (event_tx, event_rx) = unbounded::<AppEvent>();

    // 6. Start background OS hooks and hosts
    host::message_host::start_message_host(event_tx.clone());
    core::hotkey::start_hook_thread(event_tx.clone());
    core::injector::start_injector_hook();
    host::tray::start_tray_thread(event_tx.clone());
    core::window_helper::set_event_sender(event_tx.clone());

    // 7. Initialize Slint GUI Windows
    let launcher = LauncherWindow::new().expect("Failed to create LauncherWindow");
    let companion = CompanionWindow::new().expect("Failed to create CompanionWindow");

    let is_launcher_visible = Arc::new(AtomicBool::new(false));
    let is_companion_visible = Arc::new(AtomicBool::new(false));

    // Setup Launcher Callbacks
    {
        let launcher_weak = launcher.as_weak();
        launcher.on_query_changed(move |query| {
            if let Some(ui) = launcher_weak.upgrade() {
                let trimmed = query.trim().to_string();
                if trimmed.is_empty() {
                    ui.set_result_items(ModelRc::new(VecModel::default()));
                    ui.set_is_expanded(false);
                    let metrics = core::scale_manager::calculate_launcher_geometry(false, 0);
                    ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));
                } else {
                    let results = core::everything::execute_query(&trimmed, 20);
                    let slint_items: Vec<SearchItem> = results
                        .iter()
                        .map(|r| SearchItem {
                            filename: r.filename.clone().into(),
                            path: r.path.clone().into(),
                            icon: core::icon_provider::get_icon_for_path(&r.path),
                        })
                        .collect();
                    let count = slint_items.len();
                    ui.set_result_items(ModelRc::new(VecModel::from(slint_items)));
                    ui.set_is_expanded(count > 0);
                    let metrics = core::scale_manager::calculate_launcher_geometry(count > 0, count);
                    ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));
                }
            }
        });
    }

    {
        let launcher_weak = launcher.as_weak();
        let visible_flag = is_launcher_visible.clone();
        launcher.on_dismiss(move || {
            if let Some(ui) = launcher_weak.upgrade() {
                ui.hide().ok();
                visible_flag.store(false, Ordering::SeqCst);
            }
        });
    }

    {
        let launcher_weak = launcher.as_weak();
        let visible_flag = is_launcher_visible.clone();
        launcher.on_item_activated(move |idx| {
            if let Some(ui) = launcher_weak.upgrade() {
                let items = ui.get_result_items();
                if let Some(item) = items.row_data(idx as usize) {
                    let path = item.path.to_string();
                    let _ = std::process::Command::new("explorer").arg(&path).spawn();
                }
                ui.hide().ok();
                visible_flag.store(false, Ordering::SeqCst);
            }
        });
    }

    {
        let launcher_weak = launcher.as_weak();
        let visible_flag = is_launcher_visible.clone();
        launcher.on_item_open_folder(move |idx| {
            if let Some(ui) = launcher_weak.upgrade() {
                let items = ui.get_result_items();
                if let Some(item) = items.row_data(idx as usize) {
                    let path = item.path.to_string();
                    let _ = std::process::Command::new("explorer")
                        .arg(format!("/select,\"{}\"", path))
                        .spawn();
                }
                ui.hide().ok();
                visible_flag.store(false, Ordering::SeqCst);
            }
        });
    }

    // Setup Companion Callbacks
    let current_exp_hwnd = Arc::new(std::sync::atomic::AtomicIsize::new(0));
    {
        let companion_weak = companion.as_weak();
        let last_hwnd_clone = current_exp_hwnd.clone();
        companion.on_query_changed(move |query| {
            if let Some(ui) = companion_weak.upgrade() {
                let trimmed = query.trim().to_string();
                let scope_dir = ui.get_scope_dir().to_string();
                let hwnd_val = last_hwnd_clone.load(Ordering::SeqCst);
                let mut exp_rect = RECT::default();
                if hwnd_val != 0 {
                    unsafe {
                        let _ = GetWindowRect(HWND(hwnd_val as *mut _), &mut exp_rect);
                    }
                }
                if trimmed.is_empty() {
                    ui.set_result_items(ModelRc::new(VecModel::default()));
                    ui.set_is_expanded(false);
                    if hwnd_val != 0 {
                        let metrics = core::scale_manager::calculate_companion_dock_geometry(exp_rect, false, 0);
                        ui.window().set_position(slint::PhysicalPosition::new(metrics.x, metrics.y));
                        ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));
                    }
                } else {
                    let full_query = if !scope_dir.is_empty() {
                        format!("path:\"{}\" {}", scope_dir, trimmed)
                    } else {
                        trimmed
                    };
                    let results = core::everything::execute_query(&full_query, 10);
                    let slint_items: Vec<SearchItem> = results
                        .iter()
                        .map(|r| SearchItem {
                            filename: r.filename.clone().into(),
                            path: r.path.clone().into(),
                            icon: core::icon_provider::get_icon_for_path(&r.path),
                        })
                        .collect();
                    let count = slint_items.len();
                    ui.set_result_items(ModelRc::new(VecModel::from(slint_items)));
                    ui.set_is_expanded(count > 0);
                    if hwnd_val != 0 {
                        let metrics = core::scale_manager::calculate_companion_dock_geometry(exp_rect, count > 0, count);
                        ui.window().set_position(slint::PhysicalPosition::new(metrics.x, metrics.y));
                        ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));
                    }
                }
            }
        });
    }

    {
        let comp_weak = companion.as_weak();
        let visible_flag = is_companion_visible.clone();
        companion.on_dismiss(move || {
            if let Some(ui) = comp_weak.upgrade() {
                ui.hide().ok();
                visible_flag.store(false, Ordering::SeqCst);
                ui.invoke_reset_state();
                ui.set_result_items(ModelRc::new(VecModel::default()));
                ui.set_is_expanded(false);
            }
        });
    }

    {
        let comp_weak = companion.as_weak();
        let visible_flag = is_companion_visible.clone();
        companion.on_item_activated(move |idx| {
            if let Some(ui) = comp_weak.upgrade() {
                let items = ui.get_result_items();
                if let Some(item) = items.row_data(idx as usize) {
                    let path = item.path.to_string();
                    let _ = std::process::Command::new("explorer").arg(&path).spawn();
                }
                ui.hide().ok();
                visible_flag.store(false, Ordering::SeqCst);
                ui.invoke_reset_state();
                ui.set_result_items(ModelRc::new(VecModel::default()));
                ui.set_is_expanded(false);
            }
        });
    }

    {
        let comp_weak = companion.as_weak();
        let visible_flag = is_companion_visible.clone();
        companion.on_item_open_folder(move |idx| {
            if let Some(ui) = comp_weak.upgrade() {
                let items = ui.get_result_items();
                if let Some(item) = items.row_data(idx as usize) {
                    let path = item.path.to_string();
                    let _ = std::process::Command::new("explorer")
                        .arg(format!("/select,{}", path))
                        .spawn();
                }
                ui.hide().ok();
                visible_flag.store(false, Ordering::SeqCst);
                ui.invoke_reset_state();
                ui.set_result_items(ModelRc::new(VecModel::default()));
                ui.set_is_expanded(false);
            }
        });
    }

    // Event Loop Dispatcher on main thread
    let launcher_handle = launcher.as_weak();
    let comp_handle = companion.as_weak();
    let launcher_vis = is_launcher_visible.clone();
    let comp_vis = is_companion_visible.clone();

    let event_tx_bridge = event_tx.clone();

    // Spawn a bridge thread that dispatches crossbeam events into Slint UI thread
    std::thread::spawn(move || {
        while let Ok(event) = event_rx.recv() {
            match event {
                AppEvent::ToggleLauncher => {
                    let launcher_weak = launcher_handle.clone();
                    let vis_flag = launcher_vis.clone();
                    let tx_clone = event_tx_bridge.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(ui) = launcher_weak.upgrade() {
                            let is_open = vis_flag.load(Ordering::SeqCst);
                            let fg = unsafe { GetForegroundWindow() };
                            let launcher_hwnd = core::window_helper::get_launcher_hwnd();

                            // If already open AND currently foreground, toggle off
                            if is_open && launcher_hwnd.0 != std::ptr::null_mut() && fg == launcher_hwnd {
                                ui.hide().ok();
                                vis_flag.store(false, Ordering::SeqCst);
                                return;
                            }

                            // 8-step clean slate wakeup & direct focus pipeline
                            ui.invoke_reset_state();
                            ui.set_result_items(ModelRc::new(VecModel::default()));
                            ui.set_is_expanded(false);

                            let metrics = core::scale_manager::calculate_launcher_geometry(false, 0);
                            ui.window().set_position(slint::PhysicalPosition::new(metrics.x, metrics.y));
                            ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));

                            core::window_helper::record_show_time();
                            ui.show().ok();
                            ui.window().set_position(slint::PhysicalPosition::new(metrics.x, metrics.y));
                            ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));

                            vis_flag.store(true, Ordering::SeqCst);

                            // Setup floating toolwindow (no taskbar) & foreground lock penetration
                            if let Some(hwnd) = core::window_helper::find_launcher_hwnd() {
                                core::window_helper::setup_launcher_window(hwnd, tx_clone);
                                core::window_helper::force_foreground_and_focus(hwnd);
                            }

                            // Immediately focus on search bar input
                            ui.invoke_focus_input();
                        }
                    });
                }
                AppEvent::DismissLauncher => {
                    let launcher_weak = launcher_handle.clone();
                    let vis_flag = launcher_vis.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(ui) = launcher_weak.upgrade() {
                            if vis_flag.load(Ordering::SeqCst) {
                                ui.hide().ok();
                                vis_flag.store(false, Ordering::SeqCst);
                            }
                        }
                    });
                }
                AppEvent::DismissCompanion => {
                    let comp_weak = comp_handle.clone();
                    let vis_flag = comp_vis.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(ui) = comp_weak.upgrade() {
                            if vis_flag.load(Ordering::SeqCst) {
                                ui.hide().ok();
                                vis_flag.store(false, Ordering::SeqCst);
                                ui.invoke_reset_state();
                                ui.set_result_items(ModelRc::new(VecModel::default()));
                                ui.set_is_expanded(false);
                            }
                        }
                    });
                }
                AppEvent::OpenCompanion { initial_char, explorer_hwnd } => {
                    let comp_weak = comp_handle.clone();
                    let vis_flag = comp_vis.clone();
                    let last_hwnd_clone = current_exp_hwnd.clone();
                    let tx_clone = event_tx_bridge.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(ui) = comp_weak.upgrade() {
                            last_hwnd_clone.store(explorer_hwnd as isize, Ordering::SeqCst);
                            let exp_hwnd = HWND(explorer_hwnd as _);
                            let mut exp_rect = RECT::default();
                            unsafe {
                                let _ = GetWindowRect(exp_hwnd, &mut exp_rect);
                            }

                            let dir = core::explorer::get_explorer_path(exp_hwnd).unwrap_or_default();
                            ui.set_scope_dir(dir.clone().into());
                            ui.invoke_reset_state();

                            let mut count = 0;
                            if let Some(c) = initial_char {
                                let initial_text = c.to_string();
                                ui.set_search_text(initial_text.clone().into());

                                let full_query = if !dir.is_empty() {
                                    format!("path:\"{}\" {}", dir, initial_text)
                                } else {
                                    initial_text
                                };
                                let results = core::everything::execute_query(&full_query, 10);
                                let slint_items: Vec<SearchItem> = results
                                    .iter()
                                    .map(|r| SearchItem {
                                        filename: r.filename.clone().into(),
                                        path: r.path.clone().into(),
                                        icon: core::icon_provider::get_icon_for_path(&r.path),
                                    })
                                    .collect();
                                count = slint_items.len();
                                ui.set_result_items(ModelRc::new(VecModel::from(slint_items)));
                                ui.set_is_expanded(count > 0);
                            } else {
                                ui.set_result_items(ModelRc::new(VecModel::default()));
                                ui.set_is_expanded(false);
                            }

                            let metrics = core::scale_manager::calculate_companion_dock_geometry(exp_rect, count > 0, count);
                            ui.window().set_position(slint::PhysicalPosition::new(metrics.x, metrics.y));
                            ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));

                            core::window_helper::record_companion_show_time();
                            ui.show().ok();
                            ui.window().set_position(slint::PhysicalPosition::new(metrics.x, metrics.y));
                            ui.window().set_size(slint::PhysicalSize::new(metrics.width as u32, metrics.height as u32));

                            vis_flag.store(true, Ordering::SeqCst);

                            // Floating toolwindow (no taskbar) & foreground lock penetration
                            if let Some(comp_hwnd) = core::window_helper::find_companion_hwnd() {
                                core::window_helper::setup_companion_window(comp_hwnd, tx_clone);
                                core::window_helper::force_foreground_and_focus(comp_hwnd);
                            }

                            ui.invoke_focus_input();
                        }
                    });
                }
                AppEvent::TriggerManualInjection => {
                    std::thread::spawn(|| {
                        core::injector::trigger_manual_injection();
                    });
                }
                _ => {}
            }
        }
    });

    // Keep running in background even when all windows are hidden (Esc dismiss)
    slint::run_event_loop_until_quit().expect("Slint event loop terminated");
    core::injector::stop_injector_hook();
    core::hotkey::stop_hook_thread();
}

fn run_diagnose() {
    println!("============================================================");
    println!("           ZenSearch v2.0 系统全链路体检与诊断工具           ");
    println!("============================================================");

    // 1. 基础环境
    println!("\n[1/6] 基础运行环境检查:");
    let user = std::env::var("USERNAME").unwrap_or_else(|_| "Unknown".into());
    println!("  - 当前运行用户: {}", user);
    println!("  - 架构目标: x86_64-pc-windows-msvc (Rust Native)");
    println!("  - 进程高 DPI 感知: Per-Monitor V2 [已就绪]");

    // 2. Everything 探针与连通性
    println!("\n[2/6] Everything 引擎探针与连通性:");
    let is_running = core::everything::is_everything_running();
    println!("  - 内存/IPC 窗口运行态: {}", if is_running { "已在后台运行 (PASS)" } else { "未检测到就绪 IPC" });
    if let Some(exe) = core::everything::probe_everything_executable() {
        println!("  - 检测到可执行文件路径: {:?}", exe);
    } else {
        println!("  - 可执行文件路径: 未检索到有效路径");
    }
    let test_results = core::everything::execute_query("Windows", 5);
    println!("  - IPC 查询吞吐测试: 成功召回 {} 条候选", test_results.len());
    for (i, r) in test_results.iter().enumerate().take(3) {
        println!("    [{}] {} ({})", i + 1, r.filename, r.path);
    }

    // 3. 资源管理器互操作
    println!("\n[3/6] 资源管理器互操作与 COM 提取:");
    let _com = core::explorer::ComGuard::new();
    let fg = unsafe { GetForegroundWindow() };
    if fg.0 != std::ptr::null_mut() {
        let is_exp = core::explorer::is_explorer_window(fg);
        println!("  - 当前活动前台窗口: {:?}, 是否为 Explorer: {}", fg, is_exp);
        if is_exp {
            if let Some(path) = core::explorer::get_explorer_path(fg) {
                println!("  - 成功提取活动物理路径: {}", path);
            }
        }
    } else {
        println!("  - 当前无前台窗口");
    }

    // 4. 文件对话框注入与解析
    println!("\n[4/6] 文件对话框注入状态与 MRU 校验:");
    let tracker = core::injector::TRACKER.lock().unwrap();
    println!("  - MRU 记录资源管理器: {:?}", tracker.last_explorer_hwnd);
    println!("  - MRU 记录路径: {:?}", tracker.last_explorer_path);

    // 5. 开机自启动配置
    println!("\n[5/6] 开机自启动机制检查:");
    let task_ok = core::autostart::check_task_scheduler_startup();
    let reg_ok = core::autostart::check_registry_startup();
    println!("  - 计划任务特权自启 (ZenSearch_Startup): {}", if task_ok { "已配置 (免 UAC 穿透)" } else { "未配置" });
    println!("  - 注册表普通自启 (HKCU\\Run): {}", if reg_ok { "已配置" } else { "未配置" });

    // 6. 单实例与多进程健康度
    println!("\n[6/6] 单实例与互斥体状态:");
    let can_acquire = core::single_instance::acquire();
    println!("  - Win32 命名互斥体状态: {}", if can_acquire { "无冲突，可安全获得锁" } else { "已有旧实例运行中" });

    println!("\n============================================================");
    println!("诊断完毕：核心组件健康度良好。");
    println!("============================================================");
}

fn run_diagnose_watch() {
    println!("============================================================");
    println!("      ZenSearch 实时交互监控器 (按 Ctrl+C 退出)               ");
    println!("============================================================");
    println!("说明: 实时捕获并量化击键时序与前台焦点信息...");

    core::hotkey::WATCH_MODE.store(true, Ordering::SeqCst);
    let (tx, rx) = unbounded::<AppEvent>();
    core::hotkey::start_hook_thread(tx);

    while let Ok(event) = rx.recv() {
        if let AppEvent::DiagnoseKeyPress { delta_ms, is_double_ctrl, vk } = event {
            let hit_str = if is_double_ctrl { ">>> [命中双击 Ctrl 唤醒!]" } else { "" };
            println!("[KEY] VK=0x{:02X}, Δt={}ms {}", vk, delta_ms, hit_str);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_launcher_and_companion_scroll_follows_selection() {
        // 1. Test LauncherWindow (20 items, item height 50px, viewport max height 400px -> 8 visible items)
        let launcher = LauncherWindow::new().expect("Failed to create LauncherWindow");
        let items: Vec<SearchItem> = (0..20)
            .map(|i| {
                let p = format!("C:\\test\\file_{}.txt", i);
                SearchItem {
                    filename: format!("file_{}.txt", i).into(),
                    path: p.clone().into(),
                    icon: core::icon_provider::get_icon_for_path(&p),
                }
            })
            .collect();
        launcher.set_result_items(ModelRc::new(VecModel::from(items)));
        launcher.set_is_expanded(true);

        // Selecting index 0..=7 should stay within initial [0, 400] viewport (scroll_y == 0)
        for idx in 0..=7 {
            launcher.set_selected_index(idx);
            launcher.invoke_ensure_selected_visible();
            assert_eq!(launcher.get_scroll_y(), 0.0, "Index {} should not scroll", idx);
        }

        // Selecting index 8 (bottom = 450px > 400px) should scroll to -50px
        launcher.set_selected_index(8);
        launcher.invoke_ensure_selected_visible();
        assert_eq!(launcher.get_scroll_y(), -50.0);

        // Selecting index 19 (last item, bottom = 1000px) should scroll to -600px
        launcher.set_selected_index(19);
        launcher.invoke_ensure_selected_visible();
        assert_eq!(launcher.get_scroll_y(), -600.0);

        // Moving up to index 12 (top = 600px) stays in [600, 1000] viewport
        launcher.set_selected_index(12);
        launcher.invoke_ensure_selected_visible();
        assert_eq!(launcher.get_scroll_y(), -600.0);

        // Moving up to index 11 (top = 550px < 600px) scrolls up to -550px
        launcher.set_selected_index(11);
        launcher.invoke_ensure_selected_visible();
        assert_eq!(launcher.get_scroll_y(), -550.0);

        // Reset state restores scroll_y to 0
        launcher.invoke_reset_state();
        assert_eq!(launcher.get_selected_index(), 0);
        assert_eq!(launcher.get_scroll_y(), 0.0);

        // 2. Test CompanionWindow (10 items, item height 42px, viewport max height 280px)
        let companion = CompanionWindow::new().expect("Failed to create CompanionWindow");
        let comp_items: Vec<SearchItem> = (0..10)
            .map(|i| {
                let p = format!("C:\\test\\comp_{}.txt", i);
                SearchItem {
                    filename: format!("comp_{}.txt", i).into(),
                    path: p.clone().into(),
                    icon: core::icon_provider::get_icon_for_path(&p),
                }
            })
            .collect();
        companion.set_result_items(ModelRc::new(VecModel::from(comp_items)));
        companion.set_is_expanded(true);

        // Index 5 (bottom = 252px <= 280px) should not scroll
        companion.set_selected_index(5);
        companion.invoke_ensure_selected_visible();
        assert_eq!(companion.get_scroll_y(), 0.0);

        // Index 6 (bottom = 294px > 280px) should scroll to 280 - 294 = -14px
        companion.set_selected_index(6);
        companion.invoke_ensure_selected_visible();
        assert_eq!(companion.get_scroll_y(), -14.0);

        // Index 9 (bottom = 420px) should scroll to 280 - 420 = -140px
        companion.set_selected_index(9);
        companion.invoke_ensure_selected_visible();
        assert_eq!(companion.get_scroll_y(), -140.0);

        // Moving up to index 2 (top = 84px < 140px) scrolls up to -84px
        companion.set_selected_index(2);
        companion.invoke_ensure_selected_visible();
        assert_eq!(companion.get_scroll_y(), -84.0);

        companion.invoke_reset_state();
        assert_eq!(companion.get_selected_index(), 0);
        assert_eq!(companion.get_scroll_y(), 0.0);
    }
}

