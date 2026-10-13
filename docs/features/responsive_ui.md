# ZenSearch 响应式与多显示器高 DPI 适配规范 (Responsive UI & Multi-DPI)

> **文档版本**：v2.0 (Rust 原生重构版)  
> **更新时间**：2026-10-12  
> **技术栈选型**：Rust 2021 + 微软 `windows` crate + Slint 原生高 DPI 渲染引擎  
> **模块定位**：跨屏幕分辨率（1080p ~ 4K / 超宽带鱼屏）与多显示器混合 DPI 环境下的界面几何推导与矢量渲染规范。  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 适配痛点剖析与设计目标

### 1. 核心适配痛点
* **高分辨率与高缩放比例下“过小”**：在 2K/4K 等高分屏（150%、200% 缩放）下，若采用固定物理像素，窗口过小难以辨识；
* **低分辨率与小屏幕设备下“过大”**：在 1080p 便携本或 1366×768 设备下，固定尺寸遮挡大半屏幕；
* **多显示器混合 DPI 错位与模糊**：用户常见“主屏 4K 150% + 副屏 1080p 100%”混合配置。跨屏呼出若缺乏 Per-Monitor DPI 感知会导致字体发虚、边距错位或坐标飞出可视区；
* **生硬的主屏居中绑定**：用户在副显示器进行操作时，呼出搜索框若盲目定位在主屏，破坏用户心流。

### 2. 设计目标
* **黄金比例约束 (Proportional Clamping)**：浮窗尺寸动态推导，始终维持在当前屏幕工作区宽度的 35%~42% 黄金占比，并在 $[560\text{px}, 920\text{px}]$ 动态区间约束；
* **全链路 Per-Monitor DPI-aware V2**：Windows 与 Slint 完全贯通原生高 DPI 缩放，字体、边距、圆角与图标全部矢量适配，零缩放模糊；
* **视线焦点智能跟随 (Focus Screen Follow)**：优先跟随当前鼠标指针所在屏幕或当前活动窗口所在屏幕居中呼出；
* **工作区避让 (Available Geometry Safe Margin)**：严格基于有效工作区（剔除任务栏占位）计算居中，无论任务栏位于屏幕底部、顶部还是侧边均不被遮挡。

---

## 二、 进程级高 DPI 感知声明 (Per-Monitor V2)

必须在 `main.rs` 创建任何 GUI 窗口前，向操作系统注册最高等级的 DPI 感知声明：

```rust
use windows::Win32::UI::HiDpi::{
    SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};

pub fn init_high_dpi_context() {
    unsafe {
        // 声明 Per-Monitor DPI Aware V2 模式
        if let Err(_) = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) {
            // 回退到 V1 模式
            windows::Win32::UI::HiDpi::SetProcessDpiAwareness(
                windows::Win32::UI::HiDpi::PROCESS_PER_MONITOR_DPI_AWARE,
            ).ok();
        }
    }
}
```

---

## 三、 动态缩放引擎 (`src/core/scale_manager.rs`) 架构

全局单例 `ScaleManager` 负责提取当前目标显示器度量并推导几何尺寸：

```
+---------------------------------------------------------------------------------------+
|                                ScaleManager 动态缩放引擎 (Rust)                        |
+---------------------------------------------------------------------------------------+
|  [ 1. 目标屏幕特征提取 (Win32 API) ]                                                  |
|       ├── GetCursorPos + MonitorFromPoint 获取当前活动屏幕句柄 (HMONITOR)             |
|       └── GetMonitorInfoW 提取有效工作区 rcWork (自动剔除任务栏)                      |
+---------------------------------------------------------------------------------------+
|  [ 2. 综合尺寸推导 (Proportional Clamping) ]                                          |
|       ├── 宽度: W = clamp(560, (W_avail * 0.38), 920)                                 |
|       └── 视线偏置: Y = rcWork.top + (H_avail * 0.22)                                 |
+---------------------------------------------------------------------------------------+
|  [ 3. Slint 声明式 DSL 适配 ]                                                         |
|       └── Slint 原生支持逻辑像素 (Logical Pixels) 与矢量缩放，零失真高画质渲染        |
+---------------------------------------------------------------------------------------+
```

### 核心尺寸计算实现 (Rust)

```rust
use windows::Win32::Foundation::{POINT, RECT};
use windows::Win32::Graphics::Gdi::{
    GetMonitorInfoW, MonitorFromPoint, MonitorFromRect, HMONITOR, MONITORINFO, MONITOR_DEFAULTTONEAREST,
};
use windows::Win32::UI::HiDpi::{
    GetDpiForMonitor, GetDpiForSystem, MDT_EFFECTIVE_DPI,
};
use windows::Win32::UI::WindowsAndMessaging::GetCursorPos;

pub struct ScreenMetrics {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
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
            RECT { left: 0, top: 0, right: 1920, bottom: 1080 }
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
            if sys_dpi > 0 { sys_dpi as f32 / 96.0 } else { 1.0 }
        }
    }
}

pub fn calculate_launcher_geometry(is_expanded: bool, result_count: usize) -> ScreenMetrics {
    let (h_monitor, work) = get_focus_monitor_and_work_area();
    let scale = get_monitor_scale_factor(h_monitor);

    let avail_w = work.right - work.left;
    let avail_h = work.bottom - work.top;

    // 逻辑像素 (DIP) 维度推导
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

    // 转换为窗口物理像素
    let phys_w = (width_logical * scale).round() as i32;
    let phys_h = (height_logical * scale).round() as i32;

    let x = work.left + (avail_w - phys_w) / 2;
    let y = work.top + (avail_h as f32 * 0.22) as i32;

    ScreenMetrics { x, y, width: phys_w, height: phys_h }
}
```

---

## 四、 Slint 声明式矢量渲染与跨屏热自愈

* **矢量化与子像素渲染**：
  * Slint 采用纯矢量与现代着色器渲染，边框圆角、阴影与图标完全不受系统缩放模糊影响；
  * 搜索图标采用 SVG 格式直接嵌入二进制，在 100%~250% 缩放下边缘锐利。
* **跨屏移动与热插拔自愈 (`WM_DPICHANGED`)**：
  * 当用户将窗口拖拽至不同缩放比的屏幕，或者系统发生分辨率热切换时，窗口过程捕获 `WM_DPICHANGED` 消息；
  * 自动重新查询新屏幕的 DPI 与工作区几何，更新 Slint 窗口的几何布局与度量缓存，实现免重启无缝自适应。

---

## 五、 关键渲染故障复盘：高 DPI 启动器只渲染上半段截断问题

### 1. 故障现象
在 2K/4K 等启用高 DPI 缩放（如 150%、175%、200% / 192 DPI）的显示设备上，用户**双击 Ctrl 呼出启动器时，搜索框只渲染出上半段**，底部输入区域与边框被直接切断，无法正常浏览完整的搜索栏。

### 2. 根因剖析 (Root Cause)
1. **Slint 逻辑像素 vs 操作系统物理像素割裂**：
   * Slint DSL 布局（`ui/launcher.slint`）中所有尺寸单位均为**逻辑像素（DIP）**。搜索栏输入框高度为 `40px`，上下内边距各为 `10px`（共 `20px`），加上边框总计必须占用至少 **`60px ~ 64px` 逻辑高度**；
   * 旧版本代码在 `scale_manager.rs` 中硬编码了 `base_height = 64`，并在 `main.rs` 中通过 `ui.window().set_size(slint::PhysicalSize::new(width, 64))` 直接当做**物理像素**传给 Slint 窗口。
2. **高 DPI 缩放下高度直接减半**：
   * 以 200% 缩放（Scale Factor = 2.0）为例：Slint 接收到 `64px` 物理高度后，内部换算得到的逻辑高度仅为 `64 / 2.0 = 32px`；
   * 所需高度为 `64px`，而实际可用高度仅有 `32px`，窗口高度被强行削减 50%，导致下半段被窗口边界硬裁切（剪裁失真）。
3. **初次唤醒前 scale_factor 未绑定**：
   * 在 Slint 窗口创建但未执行 `ui.show()` 之前，窗口句柄未挂载至具体监视器，`scale_factor` 默认报告 `1.0`，在显示前后若未二次重约束几何，会导致物理尺寸与逻辑尺寸换算发生抖动。

### 3. 修复方案 (Resolution)
1. **统一双层推导管线**：
   * **逻辑层**：在 `scale_manager.rs` 中明确定义逻辑像素基准（`base_height_logical = 64.0`，`width_logical.clamp(560.0, 920.0)`）；
   * **物理层**：通过 Win32 `GetDpiForMonitor` 动态获取当前鼠标所在监视器的实际缩放系数 `scale`，通过 `(logical * scale).round() as i32` 输出准确的物理像素尺寸（如 200% 下物理高度为 128px，换算回 Slint 逻辑像素刚好完整呈现 64px）。
2. **唤醒管线双阶段约束**：
   * 在 `main.rs` 的 `ToggleLauncher` 与 `OpenCompanion` 唤醒流程中，在 `ui.show()` 前后均绑定 `set_position` 与 `set_size`，确保即使跨显示器或初次唤醒，窗口尺寸均百分之百锁定在当前屏幕的真实 DPI 尺寸上。
3. **随行搜索栏（Companion）动态重定位与逆向向上生长**：
   * 随行搜索栏采用“结果置顶、输入栏置底”的垂直布局（`companion.slint`）；
   * 在查询展开与收起时，记录对应 Explorer HWND 并自适应重算停靠位置与展开高度：底端输入栏物理坐标固定不动，搜索结果列表向上伸展，保证高 DPI 缩放下贴边吸附与防溢出裁剪；
   * 窗口失去焦点（`WM_ACTIVATE WA_INACTIVE`）或前台窗口离开时，联动触发隐去并清空状态。

### 4. 自动化回归测试
新增回归测试文件 [`tests/test_ui_scale.rs`](file:///d:/ZenSearch/tests/test_ui_scale.rs)，包含：
* `test_high_dpi_context_and_metrics`：验证 Per-Monitor V2 初始化态；校验折叠态物理高度 $\ge 60 \times \text{scale}$ 且展开态高度大于折叠态，阻断未来可能引入的截断回归。

