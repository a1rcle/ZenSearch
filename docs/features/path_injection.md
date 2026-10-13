# ZenSearch 路径注入（Quick Switch）技术规范与测试指南

> **文档版本**：v2.0 (Rust 原生重构版)  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + 微软 `windows` crate (Win32 / UI Automation / COM)  
> **模块定位**：ZenSearch 核心生产力特性之一，解决文件打开/另存为对话框中目录跳转繁琐的高频痛点。  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 功能概述与双模协同交互

**路径注入（又称 Quick Switch / 快速切换）**用于在任意 Windows 应用程序弹出 **“打开文件”** 或 **“另存为”** 对话框时，无需层层翻找目录或手动复制粘贴长路径，即可瞬间将目标路径注入当前对话框中，完成毫秒级目录重定向。

### 1. 核心交互演进：切回自动跳转（Switch-Back Auto Switch）
传统的路径注入通常要求用户在对话框内按下快捷键。ZenSearch 将其演进为**“从资源管理器切回对话框时自动无感跳转”**的现代化心流体验：
* **心流体验（零按键流 & 全域点击触发）**：用户在文件对话框需要定位目录时，只需顺手切到资源管理器浏览或选中目标文件夹，然后**直接鼠标点击切回对话框的任意区域（包括中间文件列表空白处、左侧导航栏、顶部地址栏或底部操作区），对话框即刻自动重定向至该目录**，全程无需任何按键。
* **双模协同设计**：
  * **模式一（主模式 - 默认开启）**：**切回自动跳转**，基于焦点转移状态机自动完成；
  * **模式二（辅助模式 - 内置常驻）**：**`Ctrl + G` 手动快捷键**，当两窗口分屏并排显示无需切换焦点时，用户在对话框内任意焦点位置直接按 `Ctrl + G` 原地拉取最新路径。

---

## 二、 核心设计原则与状态跃迁状态机

### 1. 严格 MRU 定向原则 (Strict MRU Policy)
> [!IMPORTANT]
> **严格 MRU 定向原则**：  
> ZenSearch 的自动跳转**永远只跳转到上一个实际浏览/激活过的资源管理器窗口（`CabinetWClass`）下的物理路径**。  
> 坚决杜绝“盲目遍历后台打开的其它资源管理器取第一个”的模糊兜底行为。若用户未曾浏览过资源管理器或上一个浏览窗口已关闭，ZenSearch 保持静默并拒绝跳转，绝不破坏对话框自带的默认路径。

### 2. 状态跃迁精准判定 (Focus Transition Guard)
自动跳转绝非“只要对话框获得焦点就盲目注入”，而是基于严谨的**焦点转移状态机（Window Focus Transition）**：
* 触发的前提条件必须满足：**上一个前台活动窗口是资源管理器（`Explorer`）**，且**当前获得焦点的新窗口是文件对话框（`#32770`）**。
* 若用户是从浏览器网页、聊天软件、代码编辑器等切回对话框，不触发自动注入，杜绝误操作与路径篡改。

### 3. 工作原理全景流转图

```
+─────────────────────────────────+
| 阶段一：前台浏览资源管理器 A   |
| [ CabinetWClass / 活动 Tab ]    | ───( EVENT_SYSTEM_FOREGROUND )───> 记录 last_explorer_hwnd & target_path
+─────────────────────────────────+
                 │
                 ▼ (用户点击对话框任意区域: 空白处/导航栏/底栏 或 Alt+Tab 切回)
+─────────────────────────────────+
| 阶段二：前台切入文件对话框     |
| [ #32770 Common File Dialog ]   |
+─────────────────────────────────+
                 │
                 ├── 判定转移链路: [ 上一前台窗口是否为有效 Explorer A? ]
                 │         ├── 否 (如从微信/浏览器等普通应用切入) ──> [ 安全静默，不干扰当前对话框 ]
                 │         └── 是 (正向切回) ──┐
                 │                              │
                 ▼                              ▼
+─────────────────────────────────────────────────────────────────────────────+
| 阶段三：切回自动跳转引擎 (Auto Switch) / 手动 Ctrl+G 兜底                    |
+─────────────────────────────────────────────────────────────────────────────+
                 │
                 ├── [校验1] Explorer A 是否依然存活 (IsWindow)? ──> [否: 拒绝注入]
                 ├── [校验2] 幂等校验: Dialog 当前路径是否已是 target_path? ──> [是: 跳过，防闪烁]
                 │
                 └── [校验通过] ──> 全域点击兼容 Win32 消息流注入:
                          1. 在点击完成前尽早备份当前 Edit 原始文件名文本 (过滤路径残留)
                          2. 等待物理鼠标左键释放 (VK_LBUTTON 松开 + 35ms 缓冲窗口结算子控件点击)
                          3. 跨线程强制锁定焦点至 Edit (AttachThreadInput + SetFocus + EM_SETSEL)
                          4. 规范化路径 (强制附加尾部反斜杠 "\") 并通过 WM_SETTEXT 填入
                          5. 二次确认焦点锁定在 Edit 后，PostMessageW 发送原生 VK_RETURN 激发目录切换
                          6. 校验 ToolbarWindow32 地址栏完成跳转 (最多 3 次回环校验与自愈重试)
                          7. 还原原文件名 (或清理残留路径文本) 并保持输入焦点
```

---

## 三、 底层核心实现细节 (Rust 实现)

### 1. 前台焦点监听与时序维护 (`src/core/injector.rs`)
通过 `windows::Win32::UI::Accessibility::SetWinEventHook` 安装事件钩子：

```rust
use std::sync::Mutex;
use windows::Win32::Foundation::HWND;
use windows::Win32::UI::WindowsAndMessaging::{GetClassNameW, EVENT_SYSTEM_FOREGROUND};

pub struct FocusTracker {
    pub last_explorer_hwnd: Option<isize>,
    pub last_explorer_path: Option<String>,
    pub prev_foreground_hwnd: Option<isize>,
}

pub static TRACKER: Mutex<FocusTracker> = Mutex::new(FocusTracker {
    last_explorer_hwnd: None,
    last_explorer_path: None,
    prev_foreground_hwnd: None,
});
```

### 2. 对话框控件树寻址与过滤 (`find_dialog_filename_edit`)
对话框（`#32770`）内部包含多个子控件，必须精准识别文件名编辑框：
* **目标定位**：通过 `EnumChildWindows` 递归查找 `ComboBoxEx32` $\to$ `ComboBox` $\to$ `Edit`（优先匹配标准控件 ID `0x47c / 1148`、`1001`、`1152`）；
* **严格排除**：
  * 排除搜索框（`SearchEditBoxWrapperClass`）；
  * 排除顶栏隐藏地址栏输入框（控件 ID `41477`）。
* **真有效性判定**：通过 `IsWindowVisible` 与 `IsWindowEnabled` 确保控件可交互。

### 3. 全域点击兼容：物理鼠标释放等待、跨线程焦点强锁与回环自愈 (`wait_for_mouse_release` & `focus_dialog_edit`)
在 Windows `#32770`（`IFileDialog`）对话框中，窗口内部由三大区域构成：
1. **顶部导航栏/地址栏**：`WorkerW` $\to$ `ReBarWindow32` $\to$ `Address Band Root` $\to$ `Breadcrumb Parent` $\to$ `ToolbarWindow32`（及编辑态 `Edit` ID `41477`）；
2. **中上部主体区域（占比约 85%）**：`DUIViewWndClassName` $\to$ `DirectUIHWND` $\to$ `NamespaceTreeControl`（左侧导航树 `SysTreeView32`）与 `SHELLDLL_DefView` $\to$ `DirectUIHWND`（右侧文件列表与大面积空白处）；
3. **底部操作栏区域（占比约 15%）**：`#32770` 原生底栏与文件名输入框 `ComboBoxEx32` $\to$ `ComboBox` $\to$ `Edit`（ID `1148`）。

**核心痛点与机制解析**：
* **焦点路由机制（`GetFocus()`）**：在 `comdlg32.dll` 的对话框消息循环（`IShellBrowser::TranslateAcceleratorSB`）中，当收到 `VK_RETURN` 时，系统会根据当前线程的键盘焦点（`hwndFocus`）决定行为。若用户点击了**空白处（`DirectUIHWND`）**或**导航栏（`SysTreeView32` / `ToolbarWindow32`）**，键盘焦点会被这些子控件夺走，此时向失焦的 `edit_hwnd` 发送 `VK_RETURN` 会被路由给当前聚焦的 Shell 视图（在空白处无选中项时直接被吞掉忽略），导致无法跳转。
* **鼠标按下时序抢跑（`WM_LBUTTONDOWN` vs `WM_LBUTTONUP`）**：`EVENT_SYSTEM_FOREGROUND` 在鼠标左键**刚按下（Down）**激活窗口的瞬间即触发，而人类一次完整点击需要 `60~150ms`。若在鼠标尚未松开时注入，后续的 `WM_LBUTTONUP` 或 `DirectUIHWND` 框选捕获（`SetCapture`）会直接打断注入并重新夺走焦点。

**解决方案实现**：
```rust
fn wait_for_mouse_release() {
    let mut waited_ms = 0;
    unsafe {
        while (GetAsyncKeyState(VK_LBUTTON.0 as i32) as u16 & 0x8000) != 0 && waited_ms < 400 {
            sleep(Duration::from_millis(10));
            waited_ms += 10;
        }
    }
    // 留出 35ms 缓冲窗口让 DirectUIHWND / 导航栏处理完 WM_LBUTTONUP 及焦点变更
    sleep(Duration::from_millis(35));
}

fn focus_dialog_edit(dialog_hwnd: HWND, edit_hwnd: HWND) {
    const EM_SETSEL: u32 = 0x00B1;
    unsafe {
        let cur_thread = GetCurrentThreadId();
        let dlg_thread = GetWindowThreadProcessId(dialog_hwnd, None);
        let attached = if dlg_thread != 0 && dlg_thread != cur_thread {
            AttachThreadInput(cur_thread, dlg_thread, true).as_bool()
        } else {
            false
        };

        // 强制将键盘焦点拉回底部文件名编辑框（同时自动收起顶部地址栏编辑态）
        let _ = SetFocus(edit_hwnd);

        if attached {
            let _ = AttachThreadInput(cur_thread, dlg_thread, false);
        }

        let mut res: usize = 0;
        let _ = SendMessageTimeoutW(
            edit_hwnd,
            EM_SETSEL,
            WPARAM(0),
            LPARAM(-1),
            SMTO_ABORTIFHUNG | SMTO_NORMAL,
            100,
            Some(&mut res),
        );
    }
}
```
在 `inject_path_to_dialog` 中，先备份原始文件名并调用 `wait_for_mouse_release()`，随后在最多 3 次的循环中执行 `focus_dialog_edit` $\to$ `WM_SETTEXT` $\to$ `focus_dialog_edit` $\to$ `PostMessageW(VK_RETURN)`，每次间隔 `90ms` 通过 `get_dialog_current_path(dialog_hwnd)` 校验地址栏是否已到达目标路径，确保点击对话框任意位置 100% 触发跳转。

### 4. UIPI 权限隔离穿透规范
* **问题**：以管理员身份运行的软件打开的对话框，普通进程无法对其发送 `WM_SETTEXT`。
* **Rust 方案**：由 Inno Setup 安装包通过 Windows 计划任务配置 `RunLevel: HighestAvailable`，使 ZenSearch 在用户登录时以高权限静默常驻（**无 UAC 弹窗**），天然穿透 UIPI 隔离。

### 5. Windows 11 多标签页 (Tabs) 兼容
使用 `windows::Win32::UI::Accessibility::IUIAutomation` 遍历当前激活的 Tab 控件项，并与 `IShellWindows` 实例对齐，保证抓取路径 100% 对应用户当前肉眼所见的活动标签页。

---

## 四、 异常与防误触防护机制矩阵

| 异常场景 | 系统判定 | 保护行为与自愈实现 |
| :--- | :--- | :--- |
| **从非资源管理器窗口切入** | `prev_hwnd` 为 Chrome 网页 / 聊天软件 / 编辑器 | **安全静默**：不执行注入，保护对话框当前正在选择的路径。 |
| **上一个浏览过的 Explorer 已被关闭** | `IsWindow(last_explorer_hwnd)` 为 False | **安全静默**：判定记录失效，放弃注入，绝不回退到后台无关窗口。 |
| **对话框已处于目标目录** | 对话框当前 Breadcrumb 路径与目标路径一致 | **跳过注入**：幂等忽略，防止重复刷新导致焦点闪烁。 |
| **用户从未打开过资源管理器** | 无有效 MRU 记录 | **安全静默**：保持对话框现有路径不变。 |
| **快速 Alt+Tab 防抖** | 短时间内高频切换同一对窗口（< 300ms） | **防抖去重**：忽略重复事件，避免连续触发注入。 |
| **切回时点击对话框空白处或导航栏** | `DirectUIHWND`（空白处）、`SysTreeView32`（左侧树）或 `ToolbarWindow32`（顶栏）在点击时捕获鼠标并夺取键盘焦点 | **物理鼠标释放等待与跨线程焦点强锁**：轮询 `VK_LBUTTON` 等待鼠标松开并留出 `35ms` 结算窗口，通过 `AttachThreadInput` + `SetFocus(edit_hwnd)` 将焦点强制拉回文件名输入框，解除视图对 `VK_RETURN` 的吞没，并自动收起顶部地址栏编辑态。 |
| **切回时鼠标单击命中文件夹列表项** | 鼠标按下或列表项选中触发 `CDN_SELCHANGE` 引发编辑框篡改 | **前置备份与回环自愈重试**：在点击结算前尽早备份原始文件名（过滤路径字符），注入后通过 `get_dialog_current_path` 执行最多 3 次回环校验与重聚焦注入，跳转后还原文件名或清理残留路径。 |
| **路径被误判为文件名** | 缺少尾部反斜杠 | **强制反斜杠规范化**：目标路径强制格式化为 `D:\TargetDir\`。 |
| **时序竞争引发文件名被覆盖** | 粗暴延时导致按钮异步点击读取到错误文件名 | **原生回车驱动 (VK_RETURN)**：依赖聚焦态下的 `Edit` 控件原生回车状态机完成目录跳转并复原原始文件名。 |
| **虚假成功报告** | 消息已发送但对话框因焦点竞争或 UIPI 未跳转 | **真实路径回环校验**：通过 `get_dialog_current_path()` 读取 `ToolbarWindow32` 实际物理路径确认一致后方可断言成功。 |

---

## 五、 路径注入与真实跳转一致性标准测试 SOP (Testing SOP)

所有涉及路径注入模块（`src/core/injector.rs`、`src/core/explorer.rs`、`src/core/hotkey.rs`）的修改，均必须严格通过自动化回归测试。

```
+---------------------------------------------------------------------------------------------------+
|                                路径注入真实跳转一致性测试 SOP 全景                                 |
+---------------------------------------------------------------------------------------------------+
|  [ 阶段 1: 运行环境准备 ]   ──> 挂载 Default 桌面、准备测试路径集合 (根目录 / 多级目录 / 边界路径)   |
|  [ 阶段 2: 原生对话框加载 ] ──> 拉起真实 #32770 (IFileOpenDialog)，定位 DirectUIHWND / 导航栏 / Edit|
|  [ 阶段 3: 全域物理点击 ]   ──> 模拟真实鼠标左键按下(Hold 80ms)分别点击空白处、导航树、顶部地址栏   |
|  [ 阶段 4: 地址栏回环校验 ] ──> 读取 ToolbarWindow32 实际路径，执行大小写不敏感规范化强一致性断言  |
|  [ 阶段 5: 严格 MRU 检验 ]  ──> 无记录安全静默、窗口销毁即时失效、绝不无序后台随机兜底              |
|  [ 阶段 6: 自动化测试闭环 ] ──> 运行 cargo test --test test_path_injection 确保 100% PASS         |
+---------------------------------------------------------------------------------------------------+
```

### 1. 质量红线与准入准出标准
1. **绝对一致性原则**：
   - 日志输出的 `Successfully injected path into dialog: <Path>`，其 `<Path>` 必须与对话框当前真实地址栏（`ToolbarWindow32`）解析出的物理路径 **100% 严格一致**。
2. **全域点击完全覆盖**：
   - 测试必须覆盖在真实 `#32770` 对话框中物理点击**文件列表空白处 (`SHELLDLL_DefView` $\to$ `DirectUIHWND`)**、**左侧导航树 (`SysTreeView32`)**、**顶部面包屑地址栏 (`ToolbarWindow32`)** 以及**底部操作区**的场景。
3. **文件名零破坏原则**：
   - 注入前填入的原始文件名（如 `MyReport.docx`），在目录跳转完成后必须完整复原，绝不能残留路径片段或被清空。

### 2. 标准自动化测试命令

```powershell
cargo test --test test_path_injection -- --nocapture
```

**验收通过标志**：
```text
running 7 tests
test test_filename_preservation ... ok
test test_mru_closed_window_safety ... ok
test test_open_file_injection ... ok
test test_save_file_injection ... ok
test test_consecutive_path_switching ... ok
test test_com_guard_and_tracker_transition ... ok
test test_live_dialog_blank_and_navbar_focus_injection ... ok

test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.70s
```
无任何 FAIL、无死锁或真实路径不一致警告。

