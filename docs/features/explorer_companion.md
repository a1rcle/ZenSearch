# ZenSearch 资源管理器随行增强规范 (Explorer Companion)

> **文档版本**：v2.0 (Rust 原生重构版)  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + Slint UI + Win32 COM (`IShellWindows`)  
> **模块定位**：深度集成 Windows 文件资源管理器（File Explorer），实现直接打字呼出微型随行条（Type-to-Search）、当前浏览目录限定搜索以及候选列表键盘光标自动滚动跟随。  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 功能概述与交互价值

在 Windows 原生资源管理器中，用户查找当前目录下的特定文件通常需要按 `F3` 或鼠标点击右上角自带搜索框，但原生搜索索引速度缓慢且容易递归全盘。

**ZenSearch 随行伴侣 (Explorer Companion)** 赋予资源管理器无感增强体验：
1. **直接键入即搜 (Type-to-Search)**：用户聚焦在资源管理器文件列表时，直接敲击键盘输入字符，自动唤起随行微型搜索条并捕获该字符；
2. **当前目录范围限定 (Scoped Search)**：自动获取当前 Explorer 浏览的物理路径，组装 `path:"<CurrentDir>" <keywords>` 毫秒级返回匹配项；
3. **右下角智能贴边吸附**：微型搜索条贴合在当前 Explorer 窗口内部右下角，随窗口移动/缩放联动，并具备屏幕边缘防溢出裁剪；
4. **窗口失焦无感隐去 (Blur-to-Dismiss)**：当随行条失去焦点（点击外部、切换窗口）或 Explorer 离开前台时，随行条毫秒级隐去并重置状态，绝不干扰正常桌面操作；
5. **结果列表始终置于上方与键盘光标滚动跟随 (Top-Anchored Results & Scroll-Follow-Selection)**：搜索结果列表始终展示在随行输入条的**上方**，输入栏底部固定在吸附基准线上，结果列表向上生长；按 `UpArrow` / `DownArrow` 切换候选时，视口自动跟随选中项平滑滚动（首末项边界驻留不回绕）；
6. **原生上下文菜单与单键加速键无感穿透 (Context Menu Pass-through)**：深度感知资源管理器当前是否展开了上下文右键菜单（Context Menu）或系统菜单。当用户在右键菜单中按下单键快捷键（如 `r` 属性、`d` 删除、`c` 复制、`x` 剪切等）时，精准放行原生按键事件，零冲突兼顾右键高效操作与文件列表键入即搜。

---

## 二、 Type-to-Search 按键拦截与智能穿透机制

### 1. 前台环境感知
调用 `windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow()` 获取活动窗口句柄，调用 `GetClassNameW` 校验是否为 `CabinetWClass`（资源管理器主窗口）或桌面宿主 `WorkerW`/`Progman`。

### 2. 智能按键过滤与防冲突放行规范 (Rust 实现)
为杜绝破坏用户的正常操作习惯，键盘钩子对按键实施严格的**白名单放行策略**：

```
+─────────────────────────────────────────────────────────────────────────────+
|                     Explorer 内击键事件分类与分流时序                        |
+─────────────────────────────────────────────────────────────────────────────+
                               用户在 Explorer 内按下按键
                                            │
                                            ▼
                     [ 判定 1: 是否为系统快捷键 / 控制键? ]
                (Ctrl+A/C/V/X/Z, Delete, F2, F5, Alt+Left/Right 等)
                                            │
                           ┌────────────────┴────────────────┐
                        [ 是 ]                            [ 否 ]
                           │                                 │
                           ▼                                 ▼
                     【完全放行】            [ 判定 2: 焦点是否处于文本编辑控件中? ]
                  不触发随行搜索             (Address Band Root, 搜索框, 重命名 Edit)
                                                             │
                                            ┌────────────────┴────────────────┐
                                         [ 是 ]                            [ 否 ]
                                            │                                 │
                                            ▼                                 ▼
                                      【完全放行】            [ 判定 3: 焦点是否在文件列表中? ]
                                   不干扰用户正常编辑        (DirectUIHWND / SysListView32)
                                                                              │
                                                             ┌────────────────┴────────────────┐
                                                          [ 是 ]                            [ 否 ]
                                                             │                                 │
                                                             ▼                                 ▼
                                                  [ 判定 4: 字符是否有效? ]                【完全放行】
                                                  (A-Z, 0-9, 空格, IME 输入)
                                                             │
                                            ┌────────────────┴────────────────┐
                                         [ 是 ]                            [ 否 ]
                                            │                                 │
                                            ▼                                 ▼
                                   【拦截并激活随行条】                       【放行】
                              将首字符填入伴侣条作为搜索起始
```

* **完全放行规则（坚决不拦截）**：
  * 控制与系统键：`Ctrl + A/C/V/X/Z`、`Delete`、`F2`（文件重命名）、`F5`（刷新）、`Alt + 方向键` 等；
  * **上下文右键菜单与系统菜单激活态**：检测到资源管理器处于模态菜单模式（`GUI_INMENUMODE` / `GUI_POPUPMENUMODE` / `GUI_SYSTEMMENUMODE` 或拥有活动菜单句柄 `hwndMenuOwner`，以及 `#32768` 菜单窗口），单键加速键（如 `r` 属性、`d` 删除、`c` 复制、`x` 剪切等）完全放行；
  * 光标处于资源管理器原生地址栏（`Address Band Root`）或搜索框中；
  * 用户正在重命名文件（光标位于 `Edit` 控件中）。
* **拦截与唤起规则**：
  * 仅当光标位于文件列表展示区（`DirectUIHWND` 或 `SysListView32`），且既无右键菜单也无文本编辑处于激活状态，且击键为可打印可见字符或中文拼音起始键时；
  * 拦截（Eat）首个字符，瞬间在 Explorer 右下角呼出 Slint 微型随行条并将首字符填入，后续输入重定向至随行条。

### 3. 上下文菜单与系统菜单模态穿透规范 (Menu Mode Pass-through)

#### 1) 交互设计规范
Windows 资源管理器深度依赖右键上下文菜单的**单字母加速键（Menu Access Keys / Accelerators）**进行盲操，例如：
* 选中文件后鼠标右键 + `r`：快速呼出文件“属性 (Properties)”窗口；
* 鼠标右键 + `d`：执行“删除 (Delete)”；
* 鼠标右键 + `c` / `x`：执行“复制 (Copy)”或“剪切 (Cut)”。

为实现零干扰集成，系统必须在“空白/列表区键入即搜”与“上下文菜单快捷操作”之间建立确定性的状态隔离。

#### 2) 线程级模态感知机制 (`GetGUIThreadInfo`)
当资源管理器弹出上下文菜单或下拉菜单时，其 UI 线程会进入 Win32 **模态菜单循环（Modal Menu Loop）**。系统调用 `windows::Win32::UI::WindowsAndMessaging::GetGUIThreadInfo` 动态读取线程级状态：

* **核心标志位（`GUITHREADINFO_FLAGS`）**：
  * `GUI_INMENUMODE` (`0x00000004`)：当前线程正处于菜单模式；
  * `GUI_POPUPMENUMODE` (`0x00000010`)：当前线程拥有激活的弹出式菜单（Pop-up Menu）；
  * `GUI_SYSTEMMENUMODE` (`0x00000008`)：当前线程处于系统窗口菜单模式（如通过 `Alt + Space` 呼出）；
  * `hwndMenuOwner`：活动菜单的拥有者窗口句柄（非空指针即表示当前有活动菜单存在）。
* **弹出菜单窗口类双保险判定（`#32768`）**：
  * 检查当前线程激活窗口 `hwndActive` 的窗口类名是否为 Win32 标准弹出菜单类 `#32768`。

#### 3) 决策管道实现 (`is_explorer_editing_or_menu_active`)
在低级键盘钩子入口（`WH_KEYBOARD_LL`）执行原子判定：
```rust
const GUI_INMENUMODE: u32 = 0x00000004;
const GUI_SYSTEMMENUMODE: u32 = 0x00000008;
const GUI_POPUPMENUMODE: u32 = 0x00000010;

fn is_explorer_editing_or_menu_active(exp_hwnd: HWND) -> bool {
    unsafe {
        use windows::Win32::UI::WindowsAndMessaging::{GetGUIThreadInfo, GUITHREADINFO};
        let mut gui = GUITHREADINFO {
            cbSize: std::mem::size_of::<GUITHREADINFO>() as u32,
            ..Default::default()
        };
        let thread_id = windows::Win32::UI::WindowsAndMessaging::GetWindowThreadProcessId(exp_hwnd, None);
        if GetGUIThreadInfo(thread_id, &mut gui).is_ok() {
            // 1. 检查是否处于菜单模式 (右键菜单、系统菜单、弹出式子菜单)
            let in_menu = (gui.flags.0 & (GUI_INMENUMODE | GUI_POPUPMENUMODE | GUI_SYSTEMMENUMODE)) != 0;
            let has_menu_owner = gui.hwndMenuOwner.0 != std::ptr::null_mut();
            if in_menu || has_menu_owner {
                return true;
            }

            // 检查当前活动窗口是否为标准上下文菜单窗口 (#32768)
            if gui.hwndActive.0 != std::ptr::null_mut() {
                let mut class_buf = [0u16; 32];
                let len = GetClassNameW(gui.hwndActive, &mut class_buf);
                let class_str = String::from_utf16_lossy(&class_buf[..len as usize]);
                if class_str == "#32768" {
                    return true;
                }
            }

            // 2. 检查焦点是否在重命名输入框、地址栏或原生搜索栏中
            if gui.hwndFocus.0 != std::ptr::null_mut() {
                let mut class_buf = [0u16; 64];
                let len = GetClassNameW(gui.hwndFocus, &mut class_buf);
                let class_str = String::from_utf16_lossy(&class_buf[..len as usize]);
                if class_str.contains("Edit") || class_str.contains("Address") || class_str.contains("SearchBox") {
                    return true;
                }
            }
        }
    }
    false
}
```

* **放行表现**：当 `is_explorer_editing_or_menu_active` 返回 `true` 时，键盘钩子直接调用 `CallNextHookEx` 放行，不吞键、不唤起随行条；
* **即搜恢复**：当右键菜单关闭（用户执行完菜单命令、按 `Esc` 或点击空白处）后，线程退出菜单模式，下次在文件列表直接敲键瞬间无缝恢复唤起随行条。

---

## 三、 当前目录范围限定检索 (Scoped Search)

### 1. 物理路径提取规范 (`src/core/explorer.rs`)
通过 `windows::Win32::System::Com` 调度 `IShellWindows`：

```rust
use windows::core::{Interface, Result};
use windows::Win32::Foundation::HWND;
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_LOCAL_SERVER,
    COINIT_APARTMENTTHREADED,
};
use windows::Win32::UI::Shell::{IShellWindows, ShellWindows};

pub struct ComGuard;
impl ComGuard {
    pub fn new() -> Self {
        unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok() };
        ComGuard
    }
}
impl Drop for ComGuard {
    fn drop(&mut self) {
        unsafe { CoUninitialize() };
    }
}

pub fn get_explorer_path(target_hwnd: HWND) -> Option<String> {
    let _guard = ComGuard::new();
    unsafe {
        let shell_windows: IShellWindows =
            CoCreateInstance(&ShellWindows, None, CLSCTX_LOCAL_SERVER).ok()?;
        let count = shell_windows.Count().ok()?;
        
        for i in 0..count {
            // 逐项遍历比对 HWND，提取 Document.Folder.Self.Path
            // 兼容 Win11 多标签页 UI Automation 树解析
        }
    }
    None
}
```

### 2. 搜索参数动态装配
获取到有效目录 `current_dir` 与用户输入关键词 `kw` 后，动态装配 Everything 语法：
```rust
let search_query = format!("path:\"{}\" {}", current_dir, kw);
crate::core::everything::execute_query(&search_query, 30);
```

---

## 四、 随行条几何吸附与边界防溢出算法 (Boundary Clamping)

随行条基于 Slint 实现无边框微型浮窗（`ui/companion.slint`），贴合在当前 Explorer 窗口内部右下角。

### 1. 随行条尺寸与倒置布局 (Top-Anchored Results & Bottom-Pinned Input)
随行条采用**结果置顶、输入栏置底**的垂直倒置布局：
```
+─────────────────────────────────────────────────────────────+
| [Slint 随行浮窗 ui/companion.slint]                          |
|                                                             |
| ┌─────────────────────────────────────────────────────────┐ │
| │ 搜索结果列表 (ListView)                                  │ │ ──> 仅当 is_expanded 为 true 时渲染
| │ 向上生长展现 (最高 280dp，随结果条数自适应高度)         │ │     始终位于输入栏上方
| │ • 结果项 1 (文件名 + 完整路径)                           │ │
| │ • 结果项 2 (文件名 + 完整路径)                           │ │
| └─────────────────────────────────────────────────────────┘ │
| ┌─────────────────────────────────────────────────────────┐ │
| │ 📁 [随行搜索当前目录 (Esc 关闭)...]                      │ │ ──> 随行输入栏始终固定在浮窗最底端
| └─────────────────────────────────────────────────────────┘ │
+─────────────────────────────────────────────────────────────+
```

* 逻辑尺寸计算：
  * 基础输入栏高度：$H_{input} = 46\text{dp}$；
  * 单条结果高度：$H_{item} = 44\text{dp}$；
  * 结果列表高度：$H_{list} = \text{if expanded } \{ \min(280.0, \text{count} \times 44.0) + 8.0 \} \text{ else } 0.0$；
  * 浮窗总逻辑高度：$H_{companion} = H_{input} + H_{list}$；
  * 浮窗宽度：$W_{companion} = \text{clamp}(400, \text{int}(W_{exp} \times 0.52), 640)$。

### 2. 贴边坐标推导与底部基准不动点推导 (Rust 算法)
由于随行条贴合在 Explorer 右下角，且结果列表位于输入框上方，采用**底边锚定（Bottom-Pinned）**：

$$Y_{target} = Y_{exp.bottom} - H_{companion} - \text{margin}$$

当搜索结果出现导致 $H_{companion}$ 向上膨胀时，窗口顶部 $Y_{target}$ 等量上移，而输入框在屏幕上的绝对垂直坐标始终满足：
$$Y_{input\_screen} = Y_{target} + H_{list} = Y_{exp.bottom} - H_{input} - \text{margin}$$
**结论**：输入框在屏幕上的绝对物理位置完全恒定不动，用户视线与打字焦点不会发生任何抖动，搜索结果在输入框上方优雅地逆向向上展开。

```rust
pub fn calculate_companion_dock_geometry(
    exp_rect: windows::Win32::Foundation::RECT,
    is_expanded: bool,
    result_count: usize,
) -> ScreenMetrics {
    let (h_monitor, work) = get_monitor_and_work_area_for_rect(&exp_rect);
    let scale = get_monitor_scale_factor(h_monitor);

    let exp_w = exp_rect.right - exp_rect.left;
    let exp_w_logical = exp_w as f32 / scale;
    let comp_w_logical = (exp_w_logical * 0.52).clamp(400.0, 640.0);

    let base_height_logical: f32 = 46.0; // 输入框底层基准高度
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

    // 锚定右下角：窗口顶部 Y 随总高度上升而向上推移，底端保持绝对静止
    let target_x = exp_rect.right - comp_w - margin;
    let target_y = exp_rect.bottom - comp_h - margin;

    // 安全裁剪，防止溢出屏幕工作区
    let safe_x = target_x.clamp(
        work.left + margin,
        (work.right - comp_w - margin).max(work.left),
    );
    let safe_y = target_y.clamp(
        work.top + margin,
        (work.bottom - comp_h - margin).max(work.top),
    );

    ScreenMetrics { x: safe_x, y: safe_y, width: comp_w, height: comp_h }
}
```

### 3. 窗口运动同步感知 (`SetWinEventHook`)
安装事件钩子追踪以下系统事件：
* `EVENT_OBJECT_LOCATIONCHANGE`：当 Explorer 被用户拖拽移动或缩放尺寸时，随行条毫秒级更新自身位置，保持紧密贴边吸附；
* `EVENT_SYSTEM_FOREGROUND`：当切换离开 Explorer 时，联动下文失焦隐去机制。

### 4. 随行候选列表键盘光标自动滚动跟随 (Scroll-Follow-Selection)
随行条展开后（最大可视高度 `280px`，单条高度 `42px`），当匹配项超过 6 条时，用户通过 `UpArrow` / `DownArrow` 导航候选列表：
* **视口双向绑定 (`content-y <=> root.scroll_y`)**：因 `ListView` 位于 `if root.is_expanded : VerticalLayout` 条件块内，在 `CompanionWindow` 顶层声明 `in-out property <length> scroll_y: 0px;` 并与 `ListView` 的 `content-y` 建立双向绑定，同时兼容鼠标滚轮与键盘方向键滚动；
* **首末项边界驻留**：在第 `0` 项按 `UpArrow` 或在末项 `result_items.length - 1` 按 `DownArrow` 时，光标停留在首/末项，不进行循环回绕；
* **最小视口滚动算法 (`ensure_selected_visible()`)**：
  * 视口高度：$H_{view} = \min(280\text{px}, N \times 44\text{px})$；
  * 选中项上下边界：$Y_{top} = \text{selected\_index} \times 42\text{px}$，$Y_{bottom} = Y_{top} + 42\text{px}$；
  * 若 $Y_{top} < -\text{scroll\_y}$（上方越界），更新 $\text{scroll\_y} = -Y_{top}$；
  * 若 $Y_{bottom} > -\text{scroll\_y} + H_{view}$（下方越界），更新 $\text{scroll\_y} = H_{view} - Y_{bottom}$；
* **滚动偏移同步归零**：在输入文本变化 `edited(text)` 及窗口隐去 `reset_state()` 时，同步将 `selected_index = 0`、`scroll_y = 0px` 复位。

### 5. 随行结果项原生图标统一呈现规范 (Native Icon Parity)
随行搜索结果条目与全局主浮窗保持 100% 视觉与体验对齐：
* **数据结构统一**：`SearchItem` 结构包含 `icon: image` 属性；
* **单例缓存复用**：直接复用 `icon_provider` 模块内存常驻的缓存图标，检索当前目录匹配结果时零额外 I/O 消耗；
* **紧凑自适应布局**：在单行 $42\text{px}$ 高度内嵌 $16\text{px} \times 16\text{px}$ 原生图标（居中于 $22\text{px}$ 衬底背景块中），精确展现当前目录及子目录中文件夹与各类文档的原生关联图标。

---

## 六、 失焦自动隐去与生命周期管理机制 (Blur-to-Dismiss & Lifecycle)

随行条作为沉浸式打字助手，必须遵循**“招之即来、挥之即去、绝不滞留”**的设计原则：

```
+─────────────────────────────────────────────────────────────────────────────+
|                    ZenSearch 随行条失焦隐去事件侦听与分流时序                 |
+─────────────────────────────────────────────────────────────────────────────+
                         用户点击外部区域 / 切换窗口
                                      │
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
      [ 路径 1: Win32 消息失焦 ]            [ 路径 2: 全局前台转移 ]
     WM_ACTIVATE (WA_INACTIVE)              EVENT_SYSTEM_FOREGROUND
                     │                                 │
                     └────────────────┬────────────────┘
                                      ▼
                      [ 判定: 距唤醒时间是否 > 200ms? ]
                                      │
                     ┌────────────────┴────────────────┐
                  [ 是 ]                            [ 否 ]
                     │                                 │
                     ▼                                 ▼
         向通道发送 DismissCompanion               【忽略防抖】
                     │                     防止显示瞬间焦点转移误判
                     ▼
         [ UI 线程 Clean Slate 隐去管线 ]
         1. ui.hide() 隐藏窗口
         2. 清空 search_text 输入
         3. 清空 result_items 并重置 is_expanded 为 false
         4. selected_index 置 0，scroll_y 置 0px (视口归顶)
         5. 重置当前绑定的 Explorer 句柄缓存
```

### 1. 窗口过程子类化拦截 `WM_ACTIVATE (WA_INACTIVE)`
为 `ZenSearch_Companion` 安装窗口过程子类化（Subclassing），精确监听失去激活状态（`WA_INACTIVE`）：
* 当用户点击资源管理器空白处、任务栏或其他应用软件时，`ZenSearch_Companion` 收到 `WM_ACTIVATE(WA_INACTIVE)`；
* 触发通道抛出 `AppEvent::DismissCompanion`。

### 2. 唤醒保护时间窗 (200ms Grace Period)
* 随行条在 `OpenCompanion` 展开时原子记录当前时间戳 `LAST_COMPANION_SHOW_MS`；
* 任何在呼出后 200ms 内到达的失焦消息一律滤除，防止 Windows 窗口管理器切换输入焦点时的短暂未激活状态导致闪退。

### 3. 全局前台窗口迁移感知 (`EVENT_SYSTEM_FOREGROUND`)
* 在 `win_event_proc` 中，若当前随行条处于显示状态，一旦新前台窗口句柄 `new_fg` 既不是 `ZenSearch_Companion` 也不是其附着的 `Explorer HWND`，立即执行隐去。

### 4. 彻底的无状态清空 (Clean Slate)
隐去时必须完整调用状态复位（`reset_state`），将 `search_text`、`is_expanded`、`selected_index` 及 `scroll_y` 全部复位，保证用户下次在资源管理器直接敲字时，呈现的是一个纯净的输入框且候选视口归顶，杜绝残留上一次的历史关键词、滚动偏移或未关闭的结果列表。
