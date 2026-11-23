# ZenSearch 极速搜索引擎与无状态主浮窗规范 (Search Engine & Launcher)

> **文档版本**：v2.0  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + Slint UI + Everything-SDK (Rust FFI)  
> **模块定位**：ZenSearch 的核心搜索入口，包含 Everything 原生 FFI 接入、随包内置守护拉起、无锁低级键盘钩子状态机、主搜索浮窗无状态清空机制、候选列表键盘光标视口滚动跟随，以及全量搜索结果窗口（More 窗口）联动与精准动作分发。  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 底层搜索引擎与 Everything-SDK 原生接入

### 1. 架构定位与零开销 FFI 绑定
* **免提权与进程解耦**：解析 NTFS USN 日志或 MFT 要求系统管理员权限常驻，会导致权限隔离与安全边界问题。Everything 以轻量系统服务或独立进程常驻，ZenSearch 通过 IPC 原生通信解耦权限。
* **原生 FFI 零开销**：在 Rust 中通过 `extern "system"` 直接绑定 `Everything64.dll` 的导出符号，调用开销 $\le 10\text{ns}$。

### 2. Rust FFI 接口声明与规范常量 (`src/core/everything.rs`)

```rust
// 绑定 Everything64.dll C 导出函数
#[link(name = "Everything64")]
extern "system" {
    fn Everything_SetSearchW(lpString: *const u16);
    fn Everything_SetRequestFlags(dwRequestFlags: u32);
    fn Everything_SetMax(dwMax: u32);
    fn Everything_SetSort(dwSortType: u32);
    fn Everything_SetMatchPath(bEnable: i32);
    fn Everything_SetMatchCase(bEnable: i32);
    fn Everything_SetMatchWholeWord(bEnable: i32);
    fn Everything_SetRegex(bEnable: i32);
    fn Everything_QueryW(bWait: i32) -> i32;
    fn Everything_GetNumResults() -> u32;
    fn Everything_GetTotResults() -> u32;
    fn Everything_GetResultFullPathNameW(nIndex: u32, lpString: *mut u16, nMaxCount: u32) -> u32;
    fn Everything_GetLastError() -> u32;
    fn Everything_IncRunCountFromFileNameW(lpFileName: *const u16) -> u32;
}

// Everything 官方标准排序枚举常量
pub const EVERYTHING_SORT_NAME_ASCENDING: u32 = 1;
pub const EVERYTHING_SORT_PATH_ASCENDING: u32 = 3;
pub const EVERYTHING_SORT_DATE_MODIFIED_DESCENDING: u32 = 14;
pub const EVERYTHING_SORT_RUN_COUNT_DESCENDING: u32 = 20;
```

### 3. 数据模型与契约 (`SearchResult` 与 `QueryOutput`)

系统采用 `QueryOutput` 结构封装检索响应，既携带候选列表，亦提供底层索引库的全局总命中数。通过实现 `Deref<Target = [SearchResult]>`，为上层 UI 调用方提供零成本切片访问体验：

```rust
#[derive(Debug, Clone)]
pub struct SearchResult {
    pub path: String,
    pub filename: String,
}

#[derive(Debug, Clone, Default)]
pub struct QueryOutput {
    pub results: Vec<SearchResult>,
    pub total_matched: u32,
}

impl std::ops::Deref for QueryOutput {
    type Target = [SearchResult];
    fn deref(&self) -> &Self::Target {
        &self.results
    }
}
```

### 4. 检索执行管道与纯净排除机制 (`execute_query`)

ZenSearch 坚持**真实磁盘物理文件优先**原则，在检索流水线中严格实施全局纯净排除规范：
* **全局屏蔽快捷方式 (`*.lnk`)**：任何目录下的快捷方式文件彻底不出现在搜索结果中，用户直达物理实体文件；
* **全局屏蔽 Recent 历史目录**：屏蔽包含 `*\AppData\Roaming\Microsoft\Windows\Recent\*` 与 `*\AppData\Roaming\Microsoft\Office\Recent\*` 的所有条目；
* **双层协同拦截体系（Defense in Depth）**：
  1. **L1 引擎层源头阻断（Query Rewriting）**：调用 `build_effective_query` 在用户查询词后自动注入 Everything 排除语法 `!*.lnk !"*\AppData\Roaming\Microsoft\Windows\Recent\*"`，由 Everything 索引在 C FFI 内部跳过，确保召回的 100~300 条候选全量由真实物理文件填满；
  2. **L2 客户端内存兜底（In-Memory Filter）**：遍历结果集时执行 `is_excluded(&path, &filename)` 双重校验，彻底拦截特殊通配场景下的残存项。

```rust
/// 检查文件是否处于全局排除名单中
pub fn is_excluded(path: &str, filename: &str) -> bool {
    let lower_name = filename.to_lowercase();
    // 1. 全局排除所有 .lnk 快捷方式
    if lower_name.ends_with(".lnk") {
        return true;
    }

    // 2. 排除 Windows 及 Office Recent 目录
    let lower_path = path.to_lowercase();
    if lower_path.contains(r"\appdata\roaming\microsoft\windows\recent")
        || lower_path.contains(r"\appdata\roaming\microsoft\office\recent")
    {
        return true;
    }

    false
}

/// 组装注入排除条件的最终 Everything 查询语句
pub fn build_effective_query(raw_query: &str) -> String {
    let trimmed = raw_query.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    // 注入全局排除规则：排除所有 .lnk 以及 Windows Recent 目录
    format!(r#"{} !*.lnk !"*\AppData\Roaming\Microsoft\Windows\Recent\*""#, trimmed)
}

pub fn execute_query(query: &str, max_results: u32) -> QueryOutput {
    let trimmed = query.trim();
    if trimmed.is_empty() {
        return QueryOutput::default();
    }

    // 使用带有排除条件的有效查询词
    let effective_query = build_effective_query(trimmed);
    let wide_query: Vec<u16> = OsStr::new(&effective_query)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    unsafe {
        // 1. 状态彻底复位，严格对齐 Everything 默认原生匹配语义：
        // MatchPath=0 时，仅包含 '\' 或 '/' 的词项自动匹配完整路径，其余词项精准匹配文件/目录名
        Everything_SetMatchCase(0);
        Everything_SetMatchWholeWord(0);
        Everything_SetRegex(0);
        Everything_SetMatchPath(0);

        // 2. 注入有效查询与请求字段
        Everything_SetSearchW(wide_query.as_ptr());
        Everything_SetRequestFlags(0x00000001 | 0x00000002); // EVERYTHING_REQUEST_FILE_NAME | EVERYTHING_REQUEST_PATH
        Everything_SetMax(max_results);

        // 3. 排序策略：运行次数降序（与 Everything 原生运行次数排序完全一致）
        Everything_SetSort(EVERYTHING_SORT_RUN_COUNT_DESCENDING);

        if Everything_QueryW(1) != 0 {
            let count = Everything_GetNumResults();
            let total_matched = Everything_GetTotResults();
            let mut results = Vec::with_capacity(count as usize);
            let mut buffer = [0u16; 1024];

            for i in 0..count {
                let len = Everything_GetResultFullPathNameW(i, buffer.as_mut_ptr(), 1024);
                if len > 0 {
                    let path = String::from_utf16_lossy(&buffer[..len as usize]);
                    let filename = std::path::Path::new(&path)
                        .file_name()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_else(|| path.clone());

                    // L2 客户端内存兜底过滤
                    if !is_excluded(&path, &filename) {
                        results.push(SearchResult { path, filename });
                    }
                }
            }
            return QueryOutput { results, total_matched };
        } else {
            let err = Everything_GetLastError();
            if err != 0 {
                tracing::warn!("Everything_QueryW failed with error code: {}", err);
            }
        }
    }

    QueryOutput::default()
}
```

### 5. Everything 原生直通检索与运行次数双向同步规范

ZenSearch 在主搜索浮窗（`LauncherWindow`）、完整结果窗口（`MoreWindow`）与资源管理器随行条（`CompanionWindow`）全场景采用**Everything 原生直通检索架构**：在保留全局纯净排除（`*.lnk` 与 `Recent` 目录）的前提下，严格对齐 Everything 原生查询语义、结果集与运行次数排序顺序，不进行破坏原始顺序的二次重排。

```
+───────────────────────────────────────────────────────────────────────────────────+
|                 ZenSearch Everything 原生直通检索与运行次数同步流水线               |
+───────────────────────────────────────────────────────────────────────────────────+
  用户输入 Query ──► L1: 查询改写注入 (!*.lnk !Recent)
                              │
                              ▼
             Everything SDK 原生索引检索 (零重排直通)
             • MatchPath=0 / MatchCase=0 / WholeWord=0 / Regex=0
             • 含 '\' 或 '/' 的词项由 Everything 原生自动匹配路径，普通词项精准匹配名称
             • Sort = EVERYTHING_SORT_RUN_COUNT_DESCENDING (20: 运行次数降序)
                              │
                              ▼
                 L2: 客户端内存兜底过滤 (is_excluded)
                              │
                              ▼
             按 Everything 原生顺序直接提交 Slint UI 渲染
             (Launcher Top 50 / More Top 500 / Companion Top 10)
                              │
                   用户回车激活 / 打开所在目录
                              │
                              ▼ (双向实时同步)
        Everything_IncRunCountFromFileNameW(&path) (Everything 运行次数 +1)
```

#### 1. 核心检索一致性契约
* **原生词项路径匹配语义 (`Everything_SetMatchPath(0)`)**：
  * 全局 `MatchPath` 保持关闭（`0`），与 Everything 默认设置保持一致；
  * 当用户输入包含路径分隔符的复合查询（如 `bi_cam\ record`）时，Everything 原生仅对含 `\` 的词项（`bi_cam\`）执行路径匹配，而对不含 `\` 的词项（`record`）精准匹配文件或目录名；
  * 大小写敏感、全字匹配与正则表达式标志均在每次查询前显式复位为 `0`，保障跨次查询完全独立。
* **原生运行次数降序排序 (`EVERYTHING_SORT_RUN_COUNT_DESCENDING`)**：
  * 底层固定使用 `EVERYTHING_SORT_RUN_COUNT_DESCENDING (20)` 排序；
  * 运行次数高的常用目标自然排在最前；运行次数相同时，遵循 Everything 原生次级排序（目录优先、名称升序），与 Everything 客户端返回顺序逐条一致。
* **零重排直通交付**：
  * `LauncherWindow` 直接调用 `execute_query(&trimmed, 50)`；
  * `MoreWindow` 通过 `execute_more_query(&trimmed, 500)` 直通调用 `execute_query(trimmed, 500)`；
  * `CompanionWindow` 拼接当前目录约束后直接调用 `execute_query(&full_query, 10)`。

#### 2. 运行次数双向同步机制 (`increment_run_count`)
为确保在 ZenSearch 与 Everything 任意一端打开文件或目录都能实时体现在双端排序中，系统通过 Everything-SDK 原生接口双向同步运行计数：

```rust
/// 递增 Everything 原生运行次数，保持 ZenSearch 与 Everything 运行次数排序实时同步
pub fn increment_run_count(path: &str) -> u32 {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return 0;
    }

    let wide_path: Vec<u16> = OsStr::new(trimmed)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    unsafe { Everything_IncRunCountFromFileNameW(wide_path.as_ptr()) }
}
```

* 当用户在 `LauncherWindow`、`MoreWindow` 或 `CompanionWindow` 中执行打开目标（`on_item_activated`）或定位所在目录（`on_item_open_folder`）时，立即调用 `core::everything::increment_run_count(&path)`，将操作行为实时写入 Everything 运行历史数据库。


---

## 二、 Everything 随包内置与环境探针自愈闭环

ZenSearch 将绿色版 `Everything.exe` 内置于 `tools/` 目录，构建环境自愈闭环：

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

### 1. 四级环境嗅探算法
1. **Level 1 (运行态窗口嗅探)**：调用 `FindWindowW(w!("EVERYTHING_TASKBAR_NOTIFICATION"), None)`，若命中说明已在后台运行，直接复用（耗时 $\le 0.5\text{ms}$）；
2. **Level 2 (注册表深度探测)**：使用 `windows::Win32::System::Registry` 查询 `HKLM` 与 `HKCU` 下的 `SOFTWARE\voidtools\Everything\InstallPath` 或 `App Paths\Everything.exe`；
3. **Level 3 (环境变量 PATH 扫描)**：通过 `std::env::var("PATH")` 扫描用户配置的绿色版；
4. **Level 4 (随包内置版终极兜底)**：探测当前程序目录下的 `tools/Everything.exe`。

### 2. 自动静默拉起与会话挂载
若探测到有效路径且未在运行：
* 调用 `std::process::Command::new(exe_path).arg("-startup").spawn()`；
* 核心参数 `-startup` 确保其常驻系统托盘并后台增量更新索引，不主动弹出搜索主窗口；
* 启动后以 100ms 间隔轮询 `FindWindowW`（最大 3 秒窗口），完成 IPC 通道对接。

---

## 三、 全局输入监听与低级键盘钩子 (WH_KEYBOARD_LL)

### 1. 零分配无锁钩子回调设计
Windows 操作系统通过 `LowLevelHooksTimeout` 严格限制钩子执行时延。键盘钩子回调遵循**零堆分配、无阻塞、无锁**原则：

```rust
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use windows::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    VK_CONTROL, VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_RCONTROL,
    VK_RMENU, VK_RSHIFT, VK_RWIN, VK_SHIFT,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, KBDLLHOOKSTRUCT, WH_KEYBOARD_LL, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
};

static LAST_CTRL_UP_MS: AtomicI64 = AtomicI64::new(0);
static CTRL_TAINTED: AtomicBool = AtomicBool::new(false);

pub unsafe extern "system" fn low_level_keyboard_proc(
    n_code: i32,
    w_param: WPARAM,
    l_param: LPARAM,
) -> LRESULT {
    // 1. KEYDOWN 阶段：维护干净按键状态
    if n_code >= 0 && (w_param.0 as u32 == WM_KEYDOWN || w_param.0 as u32 == WM_SYSKEYDOWN) {
        let kbd = *(l_param.0 as *const KBDLLHOOKSTRUCT);
        let vk = kbd.vkCode as u16;

        if vk == VK_LCONTROL.0 || vk == VK_RCONTROL.0 || vk == VK_CONTROL.0 {
            // Ctrl 按下：标记当前按键周期为干净（尚未与其他键组合）
            CTRL_TAINTED.store(false, Ordering::Relaxed);
        } else {
            // 夹杂非 Ctrl 按键（如 Ctrl+C、Ctrl+Tab）：污染当前按键周期并清空前次记录
            CTRL_TAINTED.store(true, Ordering::Relaxed);
            LAST_CTRL_UP_MS.store(0, Ordering::Relaxed);
        }
    }

    // 2. KEYUP 阶段：在第二次干净释放时触发唤醒
    if n_code >= 0 && (w_param.0 as u32 == WM_KEYUP || w_param.0 as u32 == WM_SYSKEYUP) {
        let kbd = *(l_param.0 as *const KBDLLHOOKSTRUCT);
        let vk = kbd.vkCode as u16;

        if vk == VK_LCONTROL.0 || vk == VK_RCONTROL.0 || vk == VK_CONTROL.0 {
            if !CTRL_TAINTED.load(Ordering::Relaxed) {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis() as i64;
                let prev = LAST_CTRL_UP_MS.swap(now, Ordering::Relaxed);
                let diff = now - prev;

                // 双击 Ctrl 命中区间: 50ms ~ 400ms
                if diff > 50 && diff < 400 {
                    LAST_CTRL_UP_MS.store(0, Ordering::Relaxed);
                    CTRL_TAINTED.store(true, Ordering::Relaxed);
                    // 向 UI 线程发送无阻塞唤醒信号
                    if let Ok(lock) = EVENT_SENDER.lock() {
                        if let Some(tx) = lock.as_ref() {
                            let _ = tx.try_send(AppEvent::ToggleLauncher);
                        }
                    }
                }
            }
        }
    }

    // 全量事件正常放行，严禁吞噬，保持系统级按键生命周期与输入法状态机完整
    CallNextHookEx(None, n_code, w_param, l_param)
}
```

### 2. 双击 Ctrl 状态机与按键生命周期规范
* **完整生命周期放行原则（Natural Event Flow）**：所有按键的 `WM_KEYDOWN` 与 `WM_KEYUP` 均由 `CallNextHookEx` 全量放行，绝不使用 `return LRESULT(1)` 截断。该机制保证当前前台应用与输入法（IME）获取到配对完备的击键生命周期；
* **防污染状态机（Tainted Isolation Guard）**：通过原子布尔量 `CTRL_TAINTED` 与释放时间戳 `LAST_CTRL_UP_MS` 协同工作，彻底隔绝 `Ctrl + C`、`Ctrl + V`、`Ctrl + Shift` 等组合键的误唤醒；
* **修饰键全集完整覆盖**：在全局键盘过滤与资源管理器前台判定中，修饰键严格覆盖 `VK_LCONTROL`、`VK_RCONTROL`、`VK_LMENU`、`VK_RMENU`、`VK_LWIN`、`VK_RWIN`、`VK_SHIFT`、`VK_LSHIFT`、`VK_RSHIFT` 全集合。

---

## 四、 主搜索浮窗唤醒、失焦自隐与无状态规范 (Floating Overlay & Focus Pipeline)

### 1. 浮窗形态与任务栏隐形规范 (ToolWindow Floating Overlay)
* **形态定位**：主界面为轻量级桌面悬浮层（Floating Overlay），启动后直接转入后台常驻守护，严禁主动弹窗打扰用户；
* **任务栏与 Alt+Tab 隐形**：
  * 应用 Win32 扩展样式：
    $$\text{ExStyle} = (\text{ExStyle} \mid \text{WS\_EX\_TOOLWINDOW}) \ \& \ \sim\text{WS\_EX\_APPWINDOW}$$
  * 在 Windows 任务栏无图标、`Alt + Tab` 应用切换时不占位；
* **无边框卡片悬浮**：
  * Slint 声明 `no-frame: true`、`background: transparent`、`always-on-top: true`；
  * 暗黑磨砂质感卡片与投影，浮于当前工作区焦点上方。

---

### 2. 失焦自动消失规范 (Auto-Dismiss on Unfocus)
* **判定链路**：
  1. 监听浮窗 HWND 的 `WM_ACTIVATE`（`LOWORD(wParam) == WA_INACTIVE`）或 `WM_KILLFOCUS` 消息，判定焦点脱离；
  2. 触发 `AppEvent::DismissLauncher` 派发至 UI 事件循环；
  3. 执行收起：调用 `ui.hide()`，重置原子状态 `is_launcher_visible.store(false)`，并触发无状态清空（Clean Slate），将焦点平滑归还原活动窗口。

---

### 3. 双击 Ctrl 穿透前台锁与输入法纯净状态复位 (Direct Focus Pipeline & IME State Reset)
> [!IMPORTANT]
> **前台锁穿透与输入法无干扰环境构建（Direct Focus & IME Isolation）**：Windows 操作系统具备严格的前台焦点限制保护，且跨线程激活极易引发修饰键逻辑状态滞留。ZenSearch 采用跨进程输入挂载、线程按键状态表强行复位与原子级聚焦管道，确保浮窗呼出瞬间系统键盘焦点 100% 直达搜索输入框，同时切断任何修饰键粘滞，确保单键 Shift 顺畅切换中英文模式。

```
+─────────────────────────────────────────────────────────────────────────────+
|     双击 Ctrl 跨进程前台锁穿透与输入法状态复位时序 (Direct Focus Pipeline)     |
+─────────────────────────────────────────────────────────────────────────────+
                            用户双击 Ctrl 干净释放
                                     │
                                     ▼
                     [ 低级键盘钩子状态机命中 ] (50ms~400ms)
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
                  [ 5. UI 线程修饰键状态表纯净重置 (IME 隔离) ]
                   reset_thread_modifier_keys()
                   SetKeyboardState([0; 256]) & keybd_event(VK_CONTROL, KEYUP)
                                      │
                                      ▼
                   [ 6. Win32 级别系统键盘焦点交割 ]
                  SetFocus(launcher_hwnd)
                                     │
                                     ▼
                  [ 7. Slint 内部搜索输入栏光标聚焦 ]
                  ui.invoke_focus_input() -> search_input.focus()
```

---

### 4. 唤醒无状态即时清空规范 (Stateless Clean Slate)
* **输入框物理归零**：每次呼出清空输入框文本；
* **候选模型与滚动偏移归零**：候选列表清空，`selected_index` 置 `0`，`scroll_y` 置 `0px`（列表视口回到顶部），折叠回紧凑单行搜索条（Base Height）；
* **未决任务熔断**：前序未完成的 Everything 异步检索 Token 立即取消，杜绝迟到回调覆盖当前状态；
* **原子级前台夺取**：光标稳固聚焦在空输入框首位。

---

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
                                            [ 步骤 7: 跨进程前台锁穿透与修饰键纯净重置 ]
                                            AttachThreadInput + SetForegroundWindow + reset_thread_modifier_keys()
                                                      │
                                                      ▼
                                            [ 步骤 8: 原子级光标聚焦搜索栏与输入法绑定 ]
                                            SetFocus + ui.invoke_focus_input()
```

---

### 6. Slint 声明式 UI 设计范式 (`ui/launcher.slint`)

```slint
import { LineEdit, ListView } from "std-widgets.slint";
import { Theme } from "common.slint";

export struct SearchItem {
    filename: string,
    path: string,
    icon: image,
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
                        item_touch := TouchArea {
                            clicked => {
                                root.selected_index = idx;
                                root.item_activated(idx);
                            }
                            pointer-event(event) => {
                                if (event.button == PointerEventButton.right && event.kind == PointerEventKind.up) {
                                    root.selected_index = idx;
                                    root.item_open_folder(idx);
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
```

---

### 7. 键鼠高频交互与候选列表视口滚动跟随 (Interactive Navigation & Direct Locating)

#### (1) 核心输入交互与操作映射 (Keyboard & Mouse Mapping)
ZenSearch 采用键鼠对称的一致性操作模型，兼顾全键盘效率流与鼠标指针直觉流：
* **导航与切换**：`UpArrow` / `DownArrow` 切换候选高亮条目，首末项边界驻留（不循环回绕），实时驱动列表视口滚动跟随；
* **打开/执行目标**：按 `Enter` 键或**鼠标左键单击**条目，直接打开当前高亮文件或运行目标程序（`item_activated`）；
* **定位目标所在文件夹**：按 `Ctrl + O` 或**鼠标悬停在条目上单击右键**，直接在全新独立资源管理器窗口中定位并高亮选中该目标（`item_open_folder`），无需多级右键菜单层级，实现即点即达的瞬时穿透；
* **关闭浮窗**：按 `Esc` 键仅隐藏搜索浮窗（Dismiss / Hide），保持后台守护进程常驻；
* **任意失焦**：鼠标点击浮窗外部或系统前台窗口切换，自动隐退。

#### (2) 视口滚动跟随与双向绑定设计
* **焦点与滚动解耦**：键盘焦点始终保持在顶层 `search_input`，方向键通过修改 `root.selected_index` 并调用 `ensure_selected_visible()` 驱动列表视口同步滚动；
* **双向属性绑定**：在 `LauncherWindow` 顶层声明 `in-out property <length> scroll_y: 0px;`，与内部 `ListView` 的 `content-y` 属性双向绑定（$\text{scroll\_y} \le 0\text{px}$）。用户拨动滚轮时自动回写 `root.scroll_y`，方向键导航时修改 `root.scroll_y` 驱动列表滚动。

#### (3) 最小视口滚动推导算法 (Minimal Viewport Scrolling)
* **可视区高度**：$H_{view} = \min(400\text{px}, N \times 52\text{px})$（单屏最多完整展示 8 项：$8 \times 50\text{px} = 400\text{px}$）；
* **当前选中项上下边界**：$Y_{top} = \text{selected\_index} \times 50\text{px}$，$Y_{bottom} = Y_{top} + 50\text{px}$；
* **向上越界对齐顶部**：若 $Y_{top} < -\text{scroll\_y}$，则令 $\text{scroll\_y} = -Y_{top}$；
* **向下越界对齐底部**：若 $Y_{bottom} > -\text{scroll\_y} + H_{view}$，则令 $\text{scroll\_y} = H_{view} - Y_{bottom}$；
* **可视区内保持不变**：当前选中项完全可见时保持 `scroll_y` 不变，避免界面跳动；
* **重置同步**：输入框文本变动（`edited(text)`）或浮窗复位（`reset_state()`）时，同步重置 `selected_index = 0` 与 `scroll_y = 0px`。

#### (4) 独立资源管理器窗口定位与命令行转义防护 (Independent Explorer Window)
在候选条目上按下 `Ctrl + O` 或鼠标右键单击触发目录定位时，遵循以下规范：
* **窗口独立性 (No Reuse)**：启动参数使用 `/n, /select,"<Path>"` 强制新建独立 Explorer 窗口，绝不复用现有窗口或多标签页；
* **文件与文件夹对称性**：选中文件在其父目录高亮，选中文件夹在其父目录高亮（而非直接进入目录内部）；
* **命令行原始传参 (raw_arg)**：使用 `std::os::windows::process::CommandExt::raw_arg()` 绕过 Rust 标准库 CRT 引号转义（转义为 `\"` 会导致 Explorer 解析失败而回退打开“桌面”）；
* **路径规范化清洗**：执行 `replace('/', "\\").trim_end_matches('\\')` 清洗尾部反斜杠，杜绝语法截断；
* **驱动器根目录保护**：对于 `C:\`、`D:` 等驱动器根路径，直接使用 `/n,"D:\"` 开新窗口，跳过 `/select`。

---

## 五、 后台常驻守护、系统托盘与自启架构规范 (Daemon Lifecycle, System Tray & Autostart)

### 1. 守护常驻原则 (Resident Daemon Principle)
* **后台静默常驻**：程序启动后（支持 `--silent` 开机自启），主进程转入后台系统托盘守护态（`Shell_NotifyIconW`）；
* **全局持续侦听**：常驻运行 `WH_KEYBOARD_LL` 低级键盘钩子、`SetWinEventHook` 焦点转移监控及 `ZenSearch_MessageHost` 隐藏消息窗口；
* **极速瞬时唤醒**：UI 窗口资源常驻内存（约 10MB），唤醒时仅执行属性重置与屏幕居中定位，响应时延 $\le 10\text{ms}$。

### 2. Esc 键行为隔离与事件循环保活 (Esc Dismissal Guard)
> [!IMPORTANT]
> **Esc 行为隔离**：按下 Esc 键仅执行 `ui.hide()` 收起并重置搜索浮窗，严禁终止主进程。

* **UI 回调解耦**：捕获 Esc 击键事件时仅执行隐藏与无状态复位；
* **事件循环保活**：强制采用 `slint::run_event_loop_until_quit()` 替代默认的 `run_event_loop()`，切断“无可见窗口”与“退出事件循环”之间的级联关联，确保浮窗隐藏后后台钩子与托盘持续保活；
* **退出机制严格收口**：全局仅允许系统托盘右键菜单“退出 ZenSearch”或显式终止指令（如升级安装器）终止进程。

### 3. Windows GUI 子系统与外部进程控制台隔离规范 (`windows_subsystem` & `CREATE_NO_WINDOW`)
* **GUI 子系统声明**：主程序入口显式声明 `#![windows_subsystem = "windows"]`，开机自启与常规运行时主进程不创建或绑定任何终端黑框；
* **CLI 诊断动态附着**：针对 `--diagnose`（健康体检）与 `--diagnose-watch`（击键监控）等 CLI 诊断场景，在启动初期通过 `AttachConsole(ATTACH_PARENT_PROCESS)` 动态挂载父终端控制台输出信息；
* **外部子进程控制台隔离**：在 GUI 子系统下，通过 `std::process::Command` 调用任何 Windows 外部控制台程序（如 `schtasks`）时，**必须显式配置 `CREATE_NO_WINDOW (0x08000000)` 创建标志**，禁止 Windows 系统为子进程分配或弹出临时控制台窗口：
  ```rust
  use std::os::windows::process::CommandExt;
  const CREATE_NO_WINDOW: u32 = 0x08000000;

  let mut cmd = Command::new("schtasks");
  #[cfg(windows)]
  cmd.creation_flags(CREATE_NO_WINDOW);
  let output = cmd.args(["/Query", "/TN", TASK_NAME]).output();
  ```

### 4. 原生系统托盘与右键菜单无阻塞生命周期规范 (Native System Tray & Menu Lifecycle)
* **独立宿主线程**：系统托盘（`ZenSearch_Tray_Class` / `ZenSearch_Tray_Window`）在独立后台线程运行专属 Win32 消息泵，与 Slint UI 线程完全解耦，互不干扰；
* **图标事件映射**：
  * 鼠标左键单击 / 双击（`WM_LBUTTONUP` / `WM_LBUTTONDBLCLK`）：通过 crossbeam 事件通道向 UI 线程发送 `AppEvent::ToggleLauncher` 触发浮窗唤醒；
  * 鼠标右键抬起（`WM_RBUTTONUP`）：呼出上下文操作菜单。
* **Win32 弹窗菜单焦点规范**：根据 Windows Shell 菜单最佳实践，呼出与销毁严格遵循三步走时序：
  ```rust
  // 1. 将托盘隐藏宿主窗口置于前台，使菜单拥有合法的顶级输入焦点
  let _ = SetForegroundWindow(hwnd);

  // 2. 阻塞式跟踪弹出菜单，获取用户点击项
  let cmd = TrackPopupMenu(
      hmenu,
      TPM_RETURNCMD | TPM_LEFTALIGN | TPM_BOTTOMALIGN,
      pt.x,
      pt.y,
      0,
      hwnd,
      None,
  );

  // 3. 核心失焦释放：发送空消息解除前台锁定，确保用户点击外部区域时菜单瞬间无感隐去
  let _ = PostMessageW(hwnd, WM_NULL, WPARAM(0), LPARAM(0));
  let _ = DestroyMenu(hmenu);
  ```

### 5. 开机自启动管理与轻量无感状态嗅探规范 (Autostart Architecture & Non-blocking Query)
ZenSearch 采用**双通道协同自启架构**，兼顾高特权免 UAC 穿透与常规无损读写，并通过轻量嗅探保障托盘交互零卡顿：

```
+---------------------------------------------------------------------------------------+
|                              开机自启动双通道协同机制                                  |
+---------------------------------------------------------------------------------------+
|  [ 通道 A: 注册表常规通道 ] ──> HKCU\Software\Microsoft\Windows\CurrentVersion\Run     |
|       ├── 适用场景: 用户通过托盘菜单开关自启、普通权限运行环境                       |
|       └── 核心特性: 原生 Win32 API (RegOpenKeyExW / RegQueryValueExW) 纳秒级直接读写   |
|  [ 通道 B: 计划任务特权通道 ] ──> Windows Task Scheduler (ZenSearch_Startup)           |
|       ├── 适用场景: Inno Setup 管理员安装、以最高特权 (HighestAvailable) 静默启动     |
|       └── 核心特性: 免 UAC 弹窗常驻，穿透 UIPI 向以管理员运行的应用对话框注入路径    |
+---------------------------------------------------------------------------------------+
```

* **极速无感状态判断（注册表首检短路）**：
  在托盘右键构建菜单时，`is_autostart_enabled()` 优先执行基于纯 Win32 原生 API 的注册表探测（`check_registry_startup()`），耗时 $< 0.05\text{ms}$ 且无任何子进程开销。若注册表已配置，立即短路返回 `true`，杜绝调用外部进程。
* **计划任务状态 TTL 内存缓存机制**：
  针对计划任务通道（`check_task_scheduler_startup()`），引入 60 秒 TTL 内存缓存与原子标志位（`TASK_SCHEDULER_CACHED_STATUS`）。托盘高频右键时直接读取内存布尔值，杜绝重复创建 `schtasks` 进程引发的主线程顿卡。
* **主程序启动期异步预热**：
  在 `main.rs` 启动阶段通过后台子线程异步预热计划任务自启状态，确保用户首次右键系统托盘时缓存已命中，达成系统托盘菜单 **0ms 极速呼出**：
  ```rust
  // main.rs 启动流水线中异步预热自启状态
  std::thread::spawn(|| {
      let _ = core::autostart::check_task_scheduler_startup();
  });
  ```

---

## 六、 候选列表原生多类型图标适配与零 I/O 缓存规范 (Native Shell Icons & Zero-I/O Caching)

### 1. 视觉体验目标与系统图标映射
ZenSearch 候选列表与 Windows 原生 Shell 图标体系完全对齐：
* **文件夹（Directory）**：呈现系统标准黄色文件夹图标；
* **各类关联文档**：`.txt`、`.pdf`、`.docx`、`.zip` 等精准展示用户当前系统关联软件专属图标；
* **可执行程序（.exe 等）**：精准提取呈现内嵌专属 LOGO 图标；
* **高分屏与透明通道**：保留 32 位 Alpha 通道与抗锯齿，高 DPI 环境下清晰锐利。

### 2. 零磁盘 I/O 设计准则 (Zero-I/O Caching)
由于 Everything 毫秒级检索对高频击键高度敏感，若直接读取物理磁盘提取图标，在网络共享盘（NAS）或休眠外置盘场景下会引发阻塞假死。系统遵循以下准则：
* **绝对零磁盘 I/O**：常规后缀及文件夹基于虚拟属性查询，完全不碰硬盘；
* **全生命周期单例复用**：常规文件类型图标在内存中全局只提取并缓存一份；
* **GDI 资源严格管理**：位图转换后立即释放 GDI DC 与图标句柄，杜绝句柄泄漏。

### 3. Win32 原生提取流水线 (`src/core/icon_provider.rs`)
1. **虚拟属性提取 (Virtual Query)**：
   * 调用 `SHGetFileInfoW` 时传入 `SHGFI_USEFILEATTRIBUTES | SHGFI_ICON | SHGFI_SMALLICON` 标志；
   * 路径传入虚拟名称（如 `folder`、`file.txt`），属性传入 `FILE_ATTRIBUTE_DIRECTORY` 或 `FILE_ATTRIBUTE_NORMAL`，直接从系统注册表与 Shell 内存缓存解析图标；
2. **HICON 到 32 位 RGBA 像素转换**：
   * 通过 `GetIconInfo` 获取 `hbmColor`；
   * 使用 `CreateCompatibleDC` 与 `GetDIBits`，指定 `biHeight = -height`（Top-down DIB 自顶向下排布）解压出标准 32-bit BGRA 像素序列；
   * 现代 32 位图标交换 R/B 分量转为 RGBA，老旧 24 位位图自动填充完全不透明 Alpha（$\alpha = 255$）；
   * 调用 `DeleteObject`、`DeleteDC` 和 `DestroyIcon` 清理句柄。

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
* **一级：扩展名全局常驻缓存 (`EXT_CACHE: LazyLock<RwLock<HashMap<String, CachedIcon>>>`)**：
  * 常规后缀与文件夹在内存中全生命周期仅提取一份，命中耗时仅 10~50 纳秒；
* **二级：实体可执行程序 LRU 缓存 (`EXE_CACHE: LazyLock<RwLock<HashMap<PathBuf, CachedIcon>>>`)**：
  * 针对 `.exe` 等具备独立内嵌图标的程序，按绝对路径哈希并限制上限 200 项，防止内存无序增长；
* **开机静默预热 (`icon_provider::init()`)**：
  * 随主进程启动在后台预热常见核心类型（文件夹、txt、docx、pdf、zip、png、exe 等），保障首次敲击即 100% 内存命中。

### 5. Slint 声明式组件设计 (`ui/launcher.slint`)
* `SearchItem` 声明 `icon: image`；
* 候选列表条目采用自适应图标容器：

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

---

## 七、 完整搜索结果窗口 (More 窗口) 与全量浏览规范

### 1. 架构定位与双层视口设计 (Two-tier View Architecture)
ZenSearch 全局搜索采用“快捷浮窗 + 深度全量窗口”的双层视口架构设计：
* **第一层：主搜索浮窗 (`LauncherWindow`)**：双击 `Ctrl` 毫秒级唤醒，按 Everything 原生运行次数排序直通展示前 50 项候选，保障即搜即走的轻量体验；
* **第二层：完整搜索结果窗口 (`MoreWindow`)**：当用户需要在更宽广的视口中浏览检索词匹配的所有项目时，通过下拉列表中配置的 `More` 入口一键展开独立窗口，提供全量项目展示、完整路径查看与多维动作分发。

```
+-----------------------------------------------------------------------------------+
|                        [双击 Ctrl 唤醒全局主搜索浮窗]                              |
|                          ZenSearch LauncherWindow                                 |
|   [ 🔍 输入关键词 (实时显示 Everything 原生排序 Top 50 候选)                      ] |
|   ├── Item 1 (运行次数优先第一项)                                                 |
|   ├── Item 2 ...                                                                  |
|   └── [ 🔍 查看全部匹配结果 (More)... ]  <────────── 用户点击展开全量视口           |
+-----------------------------------------------------------------------------------+
                                      │
                                      ▼ Launcher 隐藏并平滑过渡
+-----------------------------------------------------------------------------------+
|                     [完整搜索结果窗口] ZenSearch MoreWindow                         |
|  [ 🔍 继承/编辑检索词 ]                                       [ 匹配 1,520 项 ]    |
|  -------------------------------------------------------------------------------  |
|   📄 document.docx    D:\Workspace\Projects\document.docx                    ↵ 打开 |
|   📁 source_code      D:\ZenSearch\src\core                                  ↵ 打开 |
|   ... (全量匹配项目列表，全部左对齐流式排版，支持平滑滚动与键盘导航)                 |
|  -------------------------------------------------------------------------------  |
|  💡 操作提示: 鼠标左键：打开文件 / 进入目录 | 鼠标右键：打开所在文件夹 | Esc：关闭窗口    |
+-----------------------------------------------------------------------------------+
```

### 2. 触发入口与视口联动机制
* **常驻入口规范**：在 Launcher 下拉列表展开期间，候选列表底部常驻渲染 `🔍 查看全部匹配结果 (More)...` 选项卡，具备鼠标悬停色彩反馈与指针手型引导；
* **搜索词无缝传递**：点击 More 选项时，Launcher 触发 `open_more()` 回调并执行自隐，同时提取当前输入框文本无损注入 `MoreWindow` 的 `search_text` 属性，即刻触发全量检索；
* **动态重检支持**：`MoreWindow` 顶部内嵌全功能输入框，用户可直接在窗口内追加条件或更换关键词，即时更新全量检索结果；
* **窗口生命周期管理**：
  * 支持 `Esc` 键快速退出并隐藏；
  * 支持标题栏关闭按钮（`on_close_requested` 配置 `HideWindow` 模式，保持进程常驻无额外重建开销）。

### 3. 结果列表排版与对齐规范 (Result List Layout Specification)
为确保全量搜索结果在宽屏及多分辨率视口下的视觉清晰度与高信息密度，`MoreWindow` 的结果列表项遵循以下排版契约：
* **单行水平流式与全局左对齐**：每个结果项采用单行水平排布，文件图标、文件名和完整路径均以左侧为基准线自然排列。
* **文件名自适应与间距控制**：
  * **自然宽度展开**：文件名文本根据其实际字符长度自适应展开；
  * **适量留空规范**：文件名与路径之间设定为固定的 `12px` 适量间隔（`spacing: 12px`），确保路径紧随文件名后起始展示；
  * **长文件名截断保护**：为文件名设定 `max-width: 300px` 配合 `overflow: elide`，超长名称自动在末尾省略，防止压缩路径的可用可视区域。
* **路径全宽延伸展示**：路径配置 `horizontal-stretch: 1` 占满剩余视口宽度，并配置 `overflow: elide`，最大化呈现深层目录结构。
* **动态操作指示**：行尾的 `↵ 打开` 操作标签仅在当前条目处于选中（高亮）状态时展示在右侧，非选中状态隐藏，保持整洁开阔的视觉效果。
* **沉浸式无表头视口**：列表视口不设固定的分列表头，结合顶部检索栏的统计胶囊（如 `匹配 N 项`）与底部操作指引栏，将全部垂直空间提供给结果列表滚动呈现。

### 4. 项目项操作与精准动作分发规范 (Action Dispatch)
在完整结果列表中，支持对文件与目录进行差异化的交互响应：

| 触发操作 | 目标类型为「普通文件」 | 目标类型为「目录 / 文件夹」 | 底层实现与 API |
| :--- | :--- | :--- | :--- |
| **鼠标左键单击** / **回车键** | **直接打开该文件**：调用系统默认关联程序启动（如 `.pdf` 打开阅读器，`.docx` 打开 Word） | **直接进入该目录**：在 Windows 文件资源管理器（Explorer）中打开并浏览该目录 | `core::explorer::open_path(&path)`：基于 `Path::is_dir()` 判定，文件调用 `ShellExecuteW("open")`，目录调用 `Command::new("explorer").arg(path)` |
| **鼠标右键单击** | **打开所在文件夹并定位**：在 Windows 资源管理器中打开目标所在父目录，并高亮选中该文件 | **打开所在文件夹并定位**：在 Windows 资源管理器中打开该目录所在父目录，并高亮选中该文件夹 | `core::explorer::open_in_new_explorer_and_select(&path)`：基于 `/n, /select,"<path>"` 原生命令行直通 |

### 5. 数据层检索与流控性能保护
针对磁盘海量匹配场景（如检索单字母可能匹配数万文件），数据层实施保护性流控：
* **全量统计契约 (`execute_more_query`)**：
  * 调用 Everything C FFI 获取底层索引库真实命中总数 `total_matched = Everything_GetTotResults()`；
  * 直通透传 Everything 原生结果集与运行次数降序排列，保证与 Everything 主界面顺序一致；
* **内存与渲染流控**：
  * 向 Slint UI 层提交 500 项安全上限，避免瞬时构建过量 UI 节点导致主线程顿卡；
  * 状态栏与顶部徽标向用户明确展示统计指标（如 `显示前 500 项（共匹配 3,820 项）`）；
* **零 I/O 虚拟属性图标提取**：
  * 全量条目无缝复用 `core::icon_provider` 两级常驻缓存，图标加载耗时 $\le 50\text{ns}$，确保长列表百项滚动丝滑达 60FPS。

### 6. 多显示器与高 DPI 自适应几何推导
`MoreWindow` 严格遵循系统的 Per-Monitor V2 高 DPI 几何规范：
* **基准逻辑尺寸**：定义基准逻辑宽度 $900\text{px}$、逻辑高度 $620\text{px}$；
* **动态居中算法 (`core::scale_manager::calculate_more_window_geometry`)**：
  * 探测用户当前操作光标所在显示器的 `rcWork` 工作区尺寸；
  * 结合屏幕 DPI 缩放因子计算物理像素尺寸；
  * 采用屏幕中心偏移计算，确保在任意分辨率（1080P / 2K / 4K）及不同系统缩放比例（100% ~ 250%）下均精准居中展现。
