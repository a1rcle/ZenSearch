# ZenSearch 响应式与多显示器高 DPI 适配规范 (Responsive UI & Multi-DPI)

> **文档定位**：ZenSearch 跨屏幕分辨率（1080p ~ 4K / 超宽带鱼屏）与多显示器混合 DPI 环境下的界面几何推导、窗口定位与矢量渲染设计规范。  
> **技术栈选型**：Rust 2021 + Win32 API (`windows` crate) + Slint 原生高 DPI 渲染引擎  
> **实现模块**：`src/core/scale_manager.rs`、`tests/test_ui_scale.rs`  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 核心设计目标与约束原则

1. **黄金比例动态约束 (Proportional Clamping)**
   - **启动器 (Launcher)**：尺寸动态推导，宽度维持在当前屏幕有效工作区宽度的 38%，并钳位在逻辑像素区间 $[560\text{px}, 920\text{px}]$；
   - **随行搜索栏 (Companion)**：宽度动态锚定宿主资源管理器窗口宽度的 52%，并钳位在逻辑像素区间 $[400\text{px}, 640\text{px}]$。
2. **全链路 Per-Monitor DPI-aware V2**
   - 进程级声明感知最高等级 V2 DPI 模式，Windows 与 Slint 完全贯通，字体、边距、圆角与图标全部矢量适配，零缩放失真。
3. **视线焦点屏幕智能跟随 (Focus Screen Follow)**
   - 启动器优先跟随当前鼠标指针所在屏幕居中偏上（视线舒适区）呼出；随行搜索栏贴合宿主 Explorer 窗口底端吸附。
4. **工作区与任务栏避让 (Available Geometry Safe Margin)**
   - 严格基于屏幕有效工作区（`rcWork`，已剔除任务栏占位）计算定位，确保在任何任务栏位置（底部/顶部/侧边）均不被遮挡或溢出。

---

## 二、 进程级高 DPI 感知声明 (Per-Monitor V2)

GUI 窗口创建前，在 `main.rs` 入口声明最高等级 DPI 感知模式（`src/core/scale_manager.rs`）：

```rust
use windows::Win32::UI::HiDpi::{
    SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
};

pub fn init_high_dpi_context() {
    unsafe {
        if SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).is_err() {
            tracing::warn!("Per-Monitor V2 DPI awareness context failed to set; system fallback in effect");
        } else {
            tracing::info!("Per-Monitor V2 DPI awareness initialized");
        }
    }
}
```

---

## 三、 双层坐标推导管线 (Logical DIP vs Physical Pixels)

为确保 Slint DSL 内部矢量排版与 Windows 操作系统物理窗口管理器无缝对齐，系统采用严密的双层坐标推导管线：

```
+-----------------------------------------------------------------------------------------+
|                                坐标与几何推导双层管线                                   |
+-----------------------------------------------------------------------------------------+
|  [ 1. 逻辑像素层 (Slint Logical DIP) ]                                                  |
|       ├── Launcher:  Base = 64 DIP, Item = 52 DIP, ListMax = 400 DIP, W ∈ [560, 920]   |
|       └── Companion: Base = 46 DIP, Item = 44 DIP, ListMax = 280 DIP, W ∈ [400, 640]   |
+-----------------------------------------------------------------------------------------+
|                                      │                                                  |
|                       × Scale Factor (DPI / 96.0)                                       |
|                                      ▼                                                  |
+-----------------------------------------------------------------------------------------+
|  [ 2. 物理像素层 (Win32 Physical Pixels) ]                                              |
|       ├── phys_w = (logical_w * scale).round() as i32                                   |
|       ├── phys_h = (logical_h * scale).round() as i32                                   |
|       └── 下发系统: window.set_size(PhysicalSize) & window.set_position(PhysicalPosition)|
+-----------------------------------------------------------------------------------------+
```

### 唤醒管线双阶段约束机制
在窗口唤醒（`ToggleLauncher` 与 `OpenCompanion`）流程中，为防止窗口句柄未挂载时 `scale_factor` 默认报告 `1.0` 导致的瞬间失真抖动，在 `ui.show()` 前后均绑定位置与尺寸设置：
1. **预备阶段 (Pre-Show)**：推导目标屏幕几何，初次调用 `set_position` 与 `set_size`；
2. **呈现阶段 (Post-Show)**：窗口显示后二次锁定真实 DPI 几何参数，确保物理像素精准契合。

---

## 四、 动态缩放引擎 (`ScaleManager`) 几何推导算法

全局引擎（`src/core/scale_manager.rs`）统一负责屏幕特征提取与多窗口几何计算：

### 1. 独立启动器 (Launcher) 几何计算
```rust
pub fn calculate_launcher_geometry(is_expanded: bool, result_count: usize) -> ScreenMetrics {
    let (h_monitor, work) = get_focus_monitor_and_work_area();
    let scale = get_monitor_scale_factor(h_monitor);

    let avail_w = work.right - work.left;
    let avail_h = work.bottom - work.top;

    // 1. 逻辑像素 (DIP) 维度推导与黄金比例约束
    let avail_w_logical = avail_w as f32 / scale;
    let width_logical = (avail_w_logical * 0.38).clamp(560.0, 920.0);

    let base_height_logical: f32 = 64.0;
    let item_height_logical: f32 = 52.0;
    let list_height_logical = if is_expanded {
        ((result_count as f32) * item_height_logical).min(400.0) + 12.0
    } else {
        0.0
    };
    let height_logical = base_height_logical + list_height_logical;

    // 2. 转换为窗口物理像素
    let phys_w = (width_logical * scale).round() as i32;
    let phys_h = (height_logical * scale).round() as i32;

    // 3. 水平居中，视线偏置位于工作区上方 22% 处
    let x = work.left + (avail_w - phys_w) / 2;
    let y = work.top + (avail_h as f32 * 0.22) as i32;

    ScreenMetrics { x, y, width: phys_w, height: phys_h }
}
```

### 2. 随行搜索栏 (Companion) 逆向向上生长算法
随行搜索栏采用“结果列表置顶、输入栏置底”的垂直布局（`companion.slint`）：
* **底边锚定**：输入栏物理坐标贴靠宿主资源管理器窗口右下角内侧（保留 $12\text{px} \times \text{scale}$ 安全间距）；
* **逆向向上生长**：当结果展开时，底端输入栏物理坐标固定不动，窗口高度增加并联动减小顶部 $y$ 坐标（$y_{\text{target}} = \text{bottom} - \text{height} - \text{margin}$），实现平滑向上伸展；
* **工作区防溢出**：对计算所得坐标实施安全工作区（`rcWork`）钳位，杜绝超出屏幕边缘。

```rust
pub fn calculate_companion_dock_geometry(
    exp_rect: RECT,
    is_expanded: bool,
    result_count: usize,
) -> ScreenMetrics {
    let h_monitor = unsafe { MonitorFromRect(&exp_rect, MONITOR_DEFAULTTONEAREST) };
    let work = ...; // 获取对应监视器有效工作区
    let scale = get_monitor_scale_factor(h_monitor);

    let exp_w_logical = (exp_rect.right - exp_rect.left) as f32 / scale;
    let comp_w_logical = (exp_w_logical * 0.52).clamp(400.0, 640.0);

    let base_height_logical: f32 = 46.0;
    let item_height_logical: f32 = 44.0;
    let list_height_logical = if is_expanded {
        ((result_count as f32) * item_height_logical).min(280.0) + 8.0
    } else {
        0.0
    };
    let comp_h_logical = base_height_logical + list_height_logical;

    let comp_w = (comp_w_logical * scale).round() as i32;
    let comp_h = (comp_h_logical * scale).round() as i32;
    let margin = (12.0 * scale).round() as i32;

    // 锚定于宿主右下角，向上伸展
    let target_x = exp_rect.right - comp_w - margin;
    let target_y = exp_rect.bottom - comp_h - margin;

    // 严格约束在屏幕有效工作区内
    let safe_x = target_x.clamp(work.left + margin, (work.right - comp_w - margin).max(work.left));
    let safe_y = target_y.clamp(work.top + margin, (work.bottom - comp_h - margin).max(work.top));

    ScreenMetrics { x: safe_x, y: safe_y, width: comp_w, height: comp_h }
}
```

---

## 五、 Slint 矢量渲染与跨屏热自愈

1. **纯矢量与着色器渲染**
   - Slint 采用纯矢量与现代着色器渲染，边框圆角、阴影与图标完全不受系统缩放模糊影响；
   - 搜索图标采用 SVG 格式直接嵌入二进制，在 100%~250% 混合缩放环境下边缘保持绝对锐利。
2. **跨屏移动与热插拔自愈 (`WM_DPICHANGED`)**
   - 当窗口跨不同缩放比的屏幕移动或系统发生分辨率/缩放热切换时，窗口过程捕获 `WM_DPICHANGED` 消息；
   - 动态重新提取目标屏幕 DPI 与工作区几何，更新 Slint 窗口的几何布局与度量缓存，实现免重启无缝自适应。

---

## 六、 架构验证与测试规范

核心响应式度量在 [`tests/test_ui_scale.rs`](file:///d:/ZenSearch/tests/test_ui_scale.rs) 中实现自动化验证：

| 测试用例 | 验证目标与断言约束 |
| :--- | :--- |
| `test_high_dpi_context_and_metrics` | 验证 Per-Monitor V2 模式初始化；验证折叠态 Launcher 物理高度严格满足 $\ge 60 \times \text{scale}$，展开态高度严格大于折叠态；验证 Companion 物理高度 $\ge 44 \times \text{scale}$。 |
| `test_companion_bottom_anchor_and_upward_growth` | 验证 Companion 展开时高度增加、顶部 $y$ 上移，且底部锚定线（$y + \text{height}$）在展开前后保持恒定不变。 |
| `test_toolwindow_style_calculation` | 验证窗口样式包含 `WS_EX_TOOLWINDOW` 并剥离 `WS_EX_APPWINDOW`，确保无任务栏图标与干净的工具窗交互。 |
