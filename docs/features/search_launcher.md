# ZenSearch 极速搜索引擎与无状态主浮窗规范 (Search Engine & Launcher)

> **文档版本**：v2.0 (Rust 原生重构版)  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + Slint UI + Everything-SDK (Rust FFI)  
> **模块定位**：ZenSearch 的核心搜索入口，包含 Everything 原生 FFI 接入、随包内置守护拉起、无锁低级键盘钩子状态机、主搜索浮窗无状态清空机制以及候选列表键盘光标视口滚动跟随。  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 底层搜索引擎与 Everything-SDK 原生接入

### 1. 为什么采用 Rust FFI 对接 Everything-SDK？
* **免提权与进程解耦**：解析 NTFS USN 日志或 MFT 必须要求软件以系统管理员权限常驻，这会导致普通权限软件无法向其拖拽文件，且存在潜在安全边界风险。Everything 以轻量系统服务或独立进程常驻，ZenSearch 只需通过 IPC 原生通信。
* **原生 FFI 零开销**：在 Rust 中通过 `extern "system"` 直接绑定 `Everything64.dll` 的导出符号，调用开销 $\le 10\text{ns}$，无需通过 Python 动态类型或运行时封装。

### 2. Rust FFI 接口声明与调用范式 (`src/core/everything.rs`)

```rust
use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;

// 绑定 Everything64.dll C 导出函数
#[link(name = "Everything64")]
extern "system" {
    fn Everything_SetSearchW(lpString: *const u16);
    fn Everything_SetRequestFlags(dwRequestFlags: u32);
    fn Everything_SetMax(dwMax: u32);
    fn Everything_SetSort(dwSortType: u32);
    fn Everything_QueryW(bWait: i32) -> i32;
    fn Everything_GetNumResults() -> u32;
    fn Everything_GetResultFullPathNameW(nIndex: u32, lpString: *mut u16, nMaxCount: u32) -> u32;
    fn Everything_GetLastError() -> u32;
}

pub struct SearchResult {
    pub path: String,
    pub filename: String,
}

pub fn execute_query(query: &str, max_results: u32) -> Vec<SearchResult> {
    let wide_query: Vec<u16> = OsStr::new(query).encode_wide().chain(std::iter::once(0)).collect();
    unsafe {
        Everything_SetSearchW(wide_query.as_ptr());
        Everything_SetRequestFlags(0x00000001 | 0x00000002); // PATH | FILE_NAME
        Everything_SetMax(max_results);
        Everything_SetSort(13); // EVERYTHING_SORT_RUN_COUNT_DESCENDING
        
        if Everything_QueryW(1) != 0 {
            let count = Everything_GetNumResults();
            let mut results = Vec::with_capacity(count as usize);
            let mut buffer = [0u16; 1024];
            
            for i in 0..count {
                let len = Everything_GetResultFullPathNameW(i, buffer.as_mut_ptr(), 1024);
                if len > 0 {
                    let path = String::from_utf16_lossy(&buffer[..len as usize]);
                    let filename = std::path::Path::new(&path)
                        .file_name()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_default();
                    results.push(SearchResult { path, filename });
                }
            }
            return results;
        }
    }
    Vec::new()
}
```

---

## 二、 Everything 随包内置与环境探针自愈闭环

为实现安装即用的工业级体验，ZenSearch 将绿色版 `Everything.exe`（约 1.5MB）直接捆绑在安装包的 `tools/` 目录下。

```
+─────────────────────────────────────────────────────────────────────────────+
|                     ZenSearch - Everything 依赖探针与联动时序                |
+─────────────────────────────────────────────────────────────────────────────+
                               ZenSearch 启动
                                     │
                                     ▼
                      [ Level 1: 内存/窗口运行态探测 ]
                                     │
                    ┌────────────────┴────────────────┐
                 [已在运行]                        [未在运行]
                    │                                 │
                    ▼                                 ▼
           直接绑定 IPC 通道              [ Level 2~4: 多源静态安装嗅探 ]
                    │                                 │
                    │                    ┌────────────┴────────────┐
                    │                 [发现系统安装路径]        [完全未安装]
                    │                    │                         │
                    │                    ▼                         ▼
                    │           [ 优先使用系统版 ]           [ 自动拉起随包内置版 ]
                    │           Everything.exe -startup      tools/Everything.exe -startup
                    │                    │                         │
                    │                    └────────────┬────────────┘
                    │                                 │
                    │                                 ▼
                    │                         [ IPC 建立轮询 ]
                    │                          (最大 3 秒窗口)
                    │                                 │
                    └────────────────────────────────►▼
                                               ZenSearch 就绪
```

### 1. 四级环境嗅探算法 (Rust 实现)
1. **Level 1 (运行态窗口嗅探)**：调用 `FindWindowW(w!("EVERYTHING_TASKBAR_NOTIFICATION"), None)`，若命中说明已在后台运行，直接复用（耗时 $\le 0.5\text{ms}$）；
2. **Level 2 (注册表深度探测)**：使用 `windows::Win32::System::Registry` 查询 `HKLM` 与 `HKCU` 下的 `SOFTWARE\voidtools\Everything\InstallPath` 或 `App Paths\Everything.exe`；
3. **Level 3 (环境变量 PATH 扫描)**：通过标准库 `std::env::var("PATH")` 扫描用户配置的绿色版；
4. **Level 4 (随包内置版终极兜底)**：探测当前程序目录下的 `tools/Everything.exe`。

### 2. 自动静默拉起与会话挂载
若探测到有效路径且未在运行：
* 调用 `std::process::Command::new(exe_path).arg("-startup").spawn()`；
* 核心参数 `-startup` 确保其仅常驻托盘并在后台增量更新索引，**绝不弹出庞大的搜索主窗口**打扰用户；
* 启动后以 100ms 间隔轮询 `FindWindowW`（最多 3 秒），完成丝滑对接。

---

## 三、 全局输入监听与低级键盘钩子 (WH_KEYBOARD_LL)

### 1. 零分配无锁钩子回调设计
Windows 系统在用户按下按键时通过 `LowLevelHooksTimeout`（约 200~300ms）限制钩子处理时间。Rust 实现坚持**零堆分配、无阻塞、无锁**原则：

```rust
use std::sync::atomic::{AtomicI64, Ordering};
use windows::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, KBDLLHOOKSTRUCT, VK_LCONTROL, VK_RCONTROL, WM_KEYDOWN,
};

static LAST_CTRL_PRESS_MS: AtomicI64 = AtomicI64::new(0);

pub unsafe extern "system" fn low_level_keyboard_proc(
    n_code: i32,
    w_param: WPARAM,
    l_param: LPARAM,
) -> LRESULT {
    if n_code >= 0 && w_param.0 as u32 == WM_KEYDOWN {
        let kbd = *(l_param.0 as *const KBDLLHOOKSTRUCT);
        let vk = kbd.vkCode as u16;

        if vk == VK_LCONTROL.0 || vk == VK_RCONTROL.0 {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as i64;
            
            let prev = LAST_CTRL_PRESS_MS.swap(now, Ordering::Relaxed);
            let diff = now - prev;

            // 双击 Ctrl 命中区间: 50ms ~ 350ms
            if diff > 50 && diff < 350 {
                LAST_CTRL_PRESS_MS.store(0, Ordering::Relaxed);
                // 向 UI 线程发送无阻塞唤醒信号 (如 crossbeam_channel)
                crate::EVENT_CHANNEL.0.try_send(crate::AppEvent::ToggleLauncher).ok();
                // 消费该按键，防止向其他窗口继续传递
                return LRESULT(1);
            }
        } else {
            // 夹杂了其他非 Ctrl 按键（如 Ctrl+C、Ctrl+Tab），立即重置双击等待
            LAST_CTRL_PRESS_MS.store(0, Ordering::Relaxed);
        }
    }

    CallNextHookEx(None, n_code, w_param, l_param)
}
```

---

## 四、 主搜索浮窗唤醒、失焦自隐与无状态规范 (Floating Overlay & Focus Pipeline)

### 1. 浮窗形态与任务栏隐形规范 (ToolWindow Floating Overlay)
> [!IMPORTANT]
> **形态定位：ZenSearch 主界面坚决不作为普通程序窗口打开，而是纯粹的轻量级桌面悬浮层（Floating Overlay）。**

* **启动无感知**：双击运行或登录自启后直接转入后台常驻守护，**严禁主动弹窗打扰用户**；
* **任务栏与 Alt+Tab 隐形**：
  * 通过 Win32 API 对原生窗口句柄应用扩展样式：
    $$\text{ExStyle} = (\text{ExStyle} \mid \text{WS\_EX\_TOOLWINDOW}) \ \& \ \sim\text{WS\_EX\_APPWINDOW}$$
  * 效果：在 Windows 任务栏（Taskbar）中彻底无图标、在用户执行 `Alt + Tab` 应用切换时不占位，维持完全隐形；
* **无边框卡片悬浮**：
  * Slint 声明 `no-frame: true`、`background: transparent`、`always-on-top: true`；
  * 采用现代暗黑磨砂质感卡片与投影，浮于当前工作区视线焦点上方，零标题栏噪点。

---

### 2. 失焦自动消失规范 (Auto-Dismiss on Unfocus / Lost Focus)
> [!TIP]
> **心流体验：即用即走，失焦即隐。**  
> 用户在搜索浮窗唤醒后，若点击浮窗外部任意空白区、切换到其他软件或按下非本窗口操作，浮窗必须立刻无缝隐退，严禁残留遮挡用户视线。

* **失焦判定链路**：
  1. **Win32 窗口消息级监听**：监听宿主消息泵或子类化浮窗 HWND 的 `WM_ACTIVATE` 消息：
     * 当 `LOWORD(wParam) == WA_INACTIVE` 时，判定浮窗失去系统焦点；
     * 或捕获 `WM_KILLFOCUS` 消息判定按键焦点脱离；
  2. **事件总线无阻塞分发**：触发 `AppEvent::DismissLauncher` 派发至 UI 事件循环；
  3. **收起动作执行**：
     * 立即执行 `ui.hide()`；
     * 重置内部原子状态位 `is_launcher_visible.store(false)`；
     * 触发无状态清空（Clean Slate），将输入文本与候选列表物理归零，焦点平滑归还给原底层前台窗口。

---

### 3. 双击 Ctrl 穿透前台锁与搜索栏原子级强夺焦点 (Double-Ctrl Direct Focus)
> [!IMPORTANT]
> **核心痛点攻坚**：Windows 操作系统具备严格的前台焦点限制保护（Foreground Lockout）。如果仅调用普通的 `ui.show()`，会导致浮窗虽绘制在屏幕顶层，但系统键盘焦点依然被先前的后台应用（如 Chrome、VS Code、Terminal）牢牢锁定，造成“输入框没有光标闪烁、击键依然打进原来程序”的严重缺陷。

ZenSearch 落地**前台锁穿透与输入框原子聚焦 6 步流水线**，确保用户按下双击 Ctrl 的瞬间，键盘焦点 100% 稳固锁定在搜索输入框上：

```
+─────────────────────────────────────────────────────────────────────────────+
|           双击 Ctrl 跨进程前台锁穿透与搜索栏直达焦点时序 (Direct Focus Pipeline)     |
+─────────────────────────────────────────────────────────────────────────────+
                            用户双击 Ctrl 击键
                                     │
                                     ▼
                     [ 低级键盘钩子状态机命中 ] (50ms~350ms)
                                     │
                                     ▼
                  [ 1. 抓取当前宿主与前台线程 ID ]
                  fg_thread = GetWindowThreadProcessId(GetForegroundWindow())
                  cur_thread = GetCurrentThreadId()
                                     │
                                     ▼
                  [ 2. 跨进程线程输入挂载 (突破前台锁) ]
                  AttachThreadInput(cur_thread, fg_thread, true)
                                     │
                                     ▼
                  [ 3. 强制窗口置顶与激活 ]
                  BringWindowToTop(launcher_hwnd)
                  SetForegroundWindow(launcher_hwnd)
                  SetActiveWindow(launcher_hwnd)
                                     │
                                     ▼
                  [ 4. 线程输入连接解除 (恢复解耦) ]
                  AttachThreadInput(cur_thread, fg_thread, false)
                                     │
                                     ▼
                  [ 5. Win32 级别系统键盘焦点交割 ]
                  SetFocus(launcher_hwnd)
                                     │
                                     ▼
                  [ 6. Slint 内部搜索输入栏光标聚焦 ]
                  ui.invoke_focus_input() -> search_input.focus()
```

* **敲击零延迟**：经过此管道处理，唤醒后用户敲击的第一个字符 **100% 直达搜索栏**，输入框立即呈现活动光标，零丢字、零丢键，无需鼠标二次辅助。

---

### 4. 唤醒无状态即时清空规范 (Stateless Clean Slate)
桌面启动器的核心价值在于“即用即走、意图独立”。保留上一次的搜索记录会导致严重的视觉噪点并极易因敲击 Enter 导致误启动历史文件。
* **准则 1：输入框物理归零**：每次呼出必须 100% 清空输入框；
* **准则 2：候选模型与滚动偏移彻底归零**：候选列表归零，`selected_index` 置 `0`，`scroll_y` 置 `0px`（列表视口回到顶部），折叠回紧凑的单行搜索条（Base Height）；
* **准则 3：未决任务熔断**：前序输入未完成的 Everything 异步检索 Token 立即取消，杜绝迟到回调覆盖当前状态；
* **准则 4：原子级前台夺取**：光标稳固聚焦在空输入框首位。

### 5. 唤醒无状态重置 8 步管道时序

```
+─────────────────────────────────────────────────────────────────────────────+
|               ZenSearch 主搜索浮窗唤醒与无状态重置管道时序 (Clean Slate)       |
+─────────────────────────────────────────────────────────────────────────────+
              用户双击 Ctrl / 托盘呼出 / 二次实例激活唤醒
                                     │
                                     ▼
                        [ LauncherWindow::toggle ]
                                     │
                    ┌────────────────┴────────────────┐
          [ 已可见且处于活动前台 ]            [ 处于隐藏 / 失去焦点状态 ]
                    │                                 │
                    ▼                                 ▼
             执行 Toggle 收起                 [ 步骤 1: 异步查询 Token 熔断 ]
             window.hide()              query_token.cancel()
                                                      │
                                                      ▼
                                            [ 步骤 2: 防抖时钟重置 ]
                                            debounce_timer.stop()
                                                      │
                                                      ▼
                                            [ 步骤 3: 搜索文本与视口状态清空 ]
                                            ui.invoke_reset_state()
                                            (search_text="", selected_index=0, scroll_y=0px)
                                                      │
                                                      ▼
                                            [ 步骤 4: 结果模型彻底清空 ]
                                            ui.set_result_items(ModelRc::new([]))
                                                      │
                                                      ▼
                                            [ 步骤 5: 几何尺寸紧凑折叠 ]
                                            ui.set_is_expanded(false)
                                                      │
                                                      ▼
                                            [ 步骤 6: 屏幕感知与黄金视线居中 ]
                                            scale_manager::center_on_focus_screen()
                                                      │
                                                      ▼
                                            [ 步骤 7: 跨进程前台锁穿透置顶 ]
                                            AttachThreadInput + SetForegroundWindow
                                                      │
                                                      ▼
                                            [ 步骤 8: 原子级光标聚焦搜索栏 ]
                                            SetFocus + ui.invoke_focus_input()
```

### 6. Slint 声明式 UI 设计范式 (`ui/launcher.slint`)

```slint
import { LineEdit, ListView } from "std-widgets.slint";
import { Theme } from "common.slint";

export struct SearchItem {
    filename: string,
    path: string,
}

export component LauncherWindow inherits Window {
    in-out property <string> search_text: "";
    in-out property <bool> is_expanded: false;
    in-out property <int> selected_index: 0;
    in-out property <length> scroll_y: 0px;
    in property <[SearchItem]> result_items: [];

    callback query_changed(string);
    callback item_activated(int);
    callback item_open_folder(int);
    callback dismiss();

    public function ensure_selected_visible() {
        let view_h = min(400px, root.result_items.length * 52px);
        let item_top = root.selected_index * 50px;
        let item_bottom = item_top + 50px;

        if (item_top < -root.scroll_y) {
            root.scroll_y = -item_top;
        } else if (item_bottom > -root.scroll_y + view_h) {
            root.scroll_y = view_h - item_bottom;
        }
    }

    public function reset_state() {
        root.search_text = "";
        root.is_expanded = false;
        root.selected_index = 0;
        root.scroll_y = 0px;
        search_input.text = "";
        search_input.focus();
    }

    no-frame: true;
    background: transparent;
    always-on-top: true;

    Rectangle {
        background: Theme.background;
        border-radius: Theme.border-radius;

        VerticalLayout {
            padding: 10px;
            spacing: 8px;

            search_input := LineEdit {
                text <=> root.search_text;
                edited(text) => {
                    root.selected_index = 0;
                    root.scroll_y = 0px;
                    root.query_changed(text);
                }
                key-pressed(event) => {
                    if (event.text == Key.UpArrow) {
                        if (root.selected_index > 0) {
                            root.selected_index -= 1;
                            root.ensure_selected_visible();
                        }
                        return accept;
                    }
                    if (event.text == Key.DownArrow) {
                        if (root.selected_index + 1 < root.result_items.length) {
                            root.selected_index += 1;
                            root.ensure_selected_visible();
                        }
                        return accept;
                    }
                    return reject;
                }
            }

            if root.is_expanded : VerticalLayout {
                height: min(400px, root.result_items.length * 52px);
                ListView {
                    content-y <=> root.scroll_y;
                    for item[idx] in root.result_items : Rectangle {
                        height: 50px;
                        // 候选条目渲染与鼠标/键盘选择
                    }
                }
            }
        }
    }
}
```

### 7. 键盘高频交互规范与候选列表视口自动滚动跟随 (Scroll-Follow-Selection)

#### (1) 核心快捷键映射
* `UpArrow` / `DownArrow`：切换候选高亮条目，**首末项边界驻留（不循环回绕）**，并实时驱动列表视口滚动跟随；
* `Enter`：执行打开当前高亮文件或文件夹；
* `Ctrl + O`：调用 `std::process::Command::new("explorer").arg(format!("/select,\"{}\"", path)).spawn()` 在资源管理器中定位并选中目标文件；
* `Esc`：**仅关闭并隐藏搜索浮窗**（Dismiss / Hide），绝不退出后台常驻进程；
* **任意失焦**：鼠标点击浮窗外或切换窗口，自动隐退消失。

#### (2) 为什么需要显式视口滚动跟随机制？
在 Slint GUI 框架中：
1. **焦点与滚动解耦**：键盘焦点始终保持在顶部的 `search_input`（`LineEdit`）上以支持随时继续打字，按上下方向键仅修改 `root.selected_index` 属性，Slint 的 `ListView` 不会感知自定义索引属性的变化，因此默认不会自动滚动；
2. **条件块作用域隔离**：`ListView` 位于 `if root.is_expanded : VerticalLayout` 条件渲染块内，Slint 编译器禁止在组件顶层函数或外部平级控件（`LineEdit`）中直接通过元素 ID 访问条件块内的子组件属性。

#### (3) `content-y <=> scroll_y` 双向绑定与最小滚动推导算法
为突破条件作用域限制并同时兼容**鼠标滚轮滚动**与**键盘方向键导航**：
* **双向数据绑定**：在 `LauncherWindow` 顶层声明 `in-out property <length> scroll_y: 0px;`，并在 `ListView` 上建立双向绑定 `content-y <=> root.scroll_y;`（注：Slint 1.8+ 中使用标准属性 `content-y` 替代已弃用的 `viewport-y`，坐标约定为 $\text{scroll\_y} \le 0\text{px}$，列表向下滚动时 `scroll_y` 减小为负值）。用户拨动鼠标滚轮时 `ListView` 自动回写 `root.scroll_y`；按下方向键时 `ensure_selected_visible()` 更新 `root.scroll_y` 驱动 `ListView` 同步滚动。
* **最小滚动调整公式 (Minimal Viewport Scrolling)**：
  * 可视区高度：$H_{view} = \min(400\text{px}, N \times 52\text{px})$（单屏最多完整展示 8 项：$8 \times 50\text{px} = 400\text{px}$）；
  * 当前选中项上下边界：$Y_{top} = \text{selected\_index} \times 50\text{px}$，$Y_{bottom} = Y_{top} + 50\text{px}$；
  * 当前视口可见区间：$[-\text{scroll\_y},\ -\text{scroll\_y} + H_{view}]$；
  * **向上越界对齐顶部**：若 $Y_{top} < -\text{scroll\_y}$，则令 $\text{scroll\_y} = -Y_{top}$；
  * **向下越界对齐底部**：若 $Y_{bottom} > -\text{scroll\_y} + H_{view}$，则令 $\text{scroll\_y} = H_{view} - Y_{bottom}$；
  * **处于可视区内**：保持 `scroll_y` 不变，避免列表发生无意义的跳动。
* **状态重置同步**：当用户在输入框修改搜索词触发 `edited(text)` 或浮窗关闭/唤醒触发 `reset_state()` 时，同步将 `selected_index = 0` 且 `scroll_y = 0px`，确保新搜索结果始终从顶部第一项开始展示。

---

## 五、 后台常驻守护与窗口生命周期解耦规范 (Daemon Lifecycle & Esc Dismissal)

### 1. 守护常驻原则 (Resident Daemon Principle)
ZenSearch 本质是一款 Windows 系统级全局快捷启动器与生产力随行伴侣，必须彻底与“单窗口普通桌面软件”的生命周期解耦：
* **后台静默常驻**：程序启动后（无论是开机自启 `--silent` 还是用户手动拉起），主进程即刻转入系统后台守护态，常驻于 Windows 系统托盘（`Shell_NotifyIconW`）；
* **全局持续侦听**：后台无中断运行 `WH_KEYBOARD_LL` 低级键盘钩子（双击 Ctrl 快速呼出）、`SetWinEventHook` 焦点转移（资源管理器与对话框注入）以及 `ZenSearch_MessageHost` 隐藏消息窗口；
* **极速瞬时响应**：UI 窗口资源在初始化时常驻内存（常驻内存仅约 10MB），唤醒时仅做属性重置与贴边定位，启动耗时 $\le 10\text{ms}$，绝不重新冷启动。

### 2. Esc 键行为隔离准则 (Esc Dismissal Guard)
> [!IMPORTANT]
> **红线规则：按 Esc 键严禁退出程序进程！**  
> 在用户的心流模型中，按下 `Esc` 仅仅是“放弃本次搜索或收起浮窗”，视觉焦点归还给先前的活动工作区。若按 `Esc` 导致进程直接终结，将直接导致托盘消失、双击 Ctrl 快捷键全面瘫痪。

* **UI 回调解耦**：当捕捉到 `Esc` 击键事件时，仅执行 `ui.hide()` 与可见性标志位复位；
* **事件循环长保活**：Slint 框架默认的 `run_event_loop()` 会在最后一个窗口隐藏（Hide）时判定应用无可见窗体并自动终止事件循环。ZenSearch 必须强制改用 **`slint::run_event_loop_until_quit()`**，切断“无可见窗口”与“退出事件循环”之间的默认级联关系；
* **退出机制严格收口**：全局仅允许以下两条合法退出路径：
  1. 用户主动右键系统托盘图标，明确点击 **“退出 ZenSearch”** 菜单项；
  2. 外部安装器、升级包或系统注销时发送的显式终止指令（如 `taskkill /F /IM zensearch.exe`）。

### 3. Windows 子系统与无控制台黑框规范 (`windows_subsystem`)
* 采用 `#![windows_subsystem = "windows"]` 链接目标，在用户双击运行或登录自启时，**坚决不弹出任何黑色的 cmd 终端控制台窗口**；
* 针对 `--diagnose`（健康体检）与 `--diagnose-watch`（击键监控）等 CLI 诊断场景，在程序启动阶段通过 Win32 API `AttachConsole(ATTACH_PARENT_PROCESS)` 动态挂载父终端控制台，实现“作为后台常驻时零弹框，作为诊断命令时标准输出”的无缝融合。

---

## 六、 候选列表原生多类型图标适配与零 I/O 缓存规范 (Native Shell Icons & Zero-I/O Caching)

### 1. 需求背景与视觉体验目标
在早期版本中，搜索结果列表中的每个候选条目均硬编码为通用的文档符号（Emoji `📄`），缺乏对内容类型的直观辨识度。
为对齐 Windows 资源管理器、Everything 官方客户端与主流快捷启动工具的视觉体验，系统实现原生图标适配标准：
* **文件夹（Directory）**：呈现系统标准黄色文件夹图标；
* **各类关联文档**：如 `.txt` 显示用户当前关联程序（记事本/Notepad++/VSCode）的高清图标，`.pdf`、`.docx`、`.zip` 等各类文档精确展示对应软件专属图标；
* **可执行程序（.exe/.lnk）**：精准提取并呈现其软件内嵌的专属 LOGO 图标（如 Chrome、微信等自身图标）；
* **完整透明通道与高分屏适配**：保留 32 位 Alpha 通道与抗锯齿，在高 DPI 环境下清晰锐利。

### 2. 性能红线：为什么拒绝“朴素读取物理文件”？
Everything 检索性能极高（通常在 1~5ms 内返回结果）。若每次搜出 20 项结果均直接通过磁盘物理路径提取图标：
* **机械硬盘/网络共享盘（NAS）灾难**：读取休眠外置盘或网络路径将触发数十至数百毫秒的阻塞，导致用户打字输入严重掉帧假死；
* **高频 GDI 句柄泄漏与 GC 压力**：连续击键频繁分配销毁 GDI DC 与位图会造成 CPU 抖动。

因此，图标适配的核心准则是：**“绝对零磁盘 I/O + 扩展名全生命周期单例复用”**。

### 3. Win32 原生提取流水线 (`src/core/icon_provider.rs`)
通过微软官方 `windows` crate 直接对接 Win32 Shell 与 GDI API：

1. **虚拟属性提取 (Virtual Query)**：
   * 对常规扩展名（如 `.txt`, `.docx`, `.pdf`, `.zip` 等）及文件夹，调用 Win32 `SHGetFileInfoW` 时传入 `SHGFI_USEFILEATTRIBUTES | SHGFI_ICON | SHGFI_SMALLICON` 标志；
   * 路径直接传入虚拟名称（如 `folder`、`file.txt`），属性传入 `FILE_ATTRIBUTE_DIRECTORY` 或 `FILE_ATTRIBUTE_NORMAL`；
   * **零 I/O 优势**：Windows 直接从系统注册表与 Shell 内存缓存解析对应图标，**完全不碰硬盘**，单次提取耗时在微秒级。
2. **HICON 到 32 位 RGBA 像素转换**：
   * 通过 `GetIconInfo` 获取彩色位图句柄 `hbmColor`；
   * 使用 `CreateCompatibleDC` 与 `GetDIBits`，指定 `biHeight = -height`（Top-down DIB 自顶向下排布）解压出标准 32-bit BGRA 像素序列；
   * 自动探测与修复透明通道：针对具备 Alpha 通道的现代图标直接交换 R/B 分量转为 RGBA，针对老旧 24-bit 无 Alpha 位图自动将有效颜色像素标记为完全不透明（$\alpha = 255$）；
   * 严格调用 `DeleteObject`、`DeleteDC` 和 `DestroyIcon`，杜绝任何 GDI 句柄泄漏。

```rust
// 核心数据模型：线程安全与零拷贝设计
#[derive(Clone)]
pub struct CachedIcon {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

impl CachedIcon {
    pub fn to_slint_image(&self) -> slint::Image {
        let mut pixel_buffer =
            slint::SharedPixelBuffer::<slint::Rgba8Pixel>::new(self.width, self.height);
        pixel_buffer.make_mut_bytes().copy_from_slice(&self.rgba);
        slint::Image::from_rgba8(pixel_buffer)
    }
}
```

### 4. 两级内存常驻单例缓存与开机预热机制
为保障并发安全与极致性能，系统采用两级缓存架构：
* **一级：扩展名全局常驻缓存 (`EXT_CACHE: LazyLock<RwLock<HashMap<String, CachedIcon>>>`)**：
  * 常规后缀与文件夹在内存中全生命周期仅提取并保存一份；
  * 打字搜索连续命中相同类型时，仅耗时 **10~50 纳秒**，耗时可忽略不计；
* **二级：实体可执行程序 LRU 缓存 (`EXE_CACHE: LazyLock<RwLock<HashMap<PathBuf, CachedIcon>>>`)**：
  * 仅针对 `.exe`、`.lnk` 等按文件拥有独立内嵌图标的程序，按绝对路径哈希并限制上限 200 项，防止内存无序增长与磁盘重复访问；
* **开机静默预热 (`icon_provider::init()`)**：
  * 随 ZenSearch 主进程启动，在后台静默预热常见的 30 种核心类型（文件夹、txt、docx、pdf、zip、png、exe默认等），确保用户第一次敲击键盘即可享受 100% 内存即时命中。

### 5. Slint 声明式组件改造 (`ui/launcher.slint`)
* 在 `SearchItem` 结构体中新增 `icon: image` 属性；
* 列表项布局将原写死文本替换为高保真自适应组件：
  ```slint
  Rectangle {
      width: 28px;
      height: 28px;
      y: 5px;
      border-radius: 6px;
      background: idx == root.selected_index ? #3E4256 : #2D303E;

      Image {
          source: item.icon;
          width: 18px;
          height: 18px;
          image-fit: contain;
          horizontal-alignment: center;
          vertical-alignment: center;
      }
  }
  ```

