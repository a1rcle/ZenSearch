# ZenSearch 极速搜索引擎与无状态主浮窗规范 (Search Engine & Launcher)

> **文档版本**：v2.0  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + Slint UI + Everything-SDK (Rust FFI)  
> **模块定位**：ZenSearch 的核心搜索入口，包含 Everything 原生 FFI 接入、随包内置守护拉起、无锁低级键盘钩子状态机、主搜索浮窗无状态清空机制以及候选列表键盘光标视口滚动跟随。  
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

### 4. 检索执行管道与调度规范 (`execute_query`)

```rust
pub fn execute_query(query: &str, max_results: u32) -> QueryOutput {
    let trimmed = query.trim();
    if trimmed.is_empty() {
        return QueryOutput::default();
    }

    let wide_query: Vec<u16> = OsStr::new(trimmed)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    // 智能全路径匹配判定：包含空格或路径分隔符时启用全路径比对，否则按纯文件名匹配
    let enable_match_path = if trimmed.contains(' ') || trimmed.contains('\\') || trimmed.contains('/') {
        1
    } else {
        0
    };

    unsafe {
        // 1. 状态彻底复位，保障查询独立性
        Everything_SetMatchCase(0);
        Everything_SetMatchWholeWord(0);
        Everything_SetRegex(0);
        Everything_SetMatchPath(enable_match_path);

        // 2. 注入查询与请求字段
        Everything_SetSearchW(wide_query.as_ptr());
        Everything_SetRequestFlags(0x00000001 | 0x00000002); // EVERYTHING_REQUEST_FILE_NAME | EVERYTHING_REQUEST_PATH
        Everything_SetMax(max_results);

        // 3. 排序策略：修改日期降序（最新优先）
        Everything_SetSort(EVERYTHING_SORT_DATE_MODIFIED_DESCENDING);

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
                    results.push(SearchResult { path, filename });
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

### 5. 宽进精排架构与 Listary 级多维排序算法

为提供最契合直觉与肌肉记忆的候选排序，系统采用**宽进精排（Two-Stage Ranking）**架构：底层 Everything 负责毫秒级全盘粗筛，Rust 精排引擎进行多维复合评分与历史加权，输出最优 Top N。

```
+───────────────────────────────────────────────────────────────────────────────────+
|                        ZenSearch 宽进精排流水线 (Two-Stage Ranking)                |
+───────────────────────────────────────────────────────────────────────────────────+
  用户输入 Query ──► Everything SDK 粗排召回 (候选池 100~300 条)
                                │
                                ▼
                   ZenSearch 精排计算引擎 (Ranker)
       ┌────────────────────────┼────────────────────────┐
       ▼                        ▼                        ▼
  [ 文本匹配分 S_match ]   [ 自适应历史分 S_history ] [ 场景上下文分 S_type ]
  • 全字/主干完全匹配       • Query 专属强绑定       • Launcher: 可执行程序优先
  • 文件名/主干前缀匹配     • 使用频次对数加权        • Companion: 文件夹优先
  • 连续子串位置加成       • 3天半衰期指数时间衰减               │
  • 驼峰/首字母缩写匹配                  │                        ▼
       │                                │             [ 路径惩罚分 S_path ]
       │                                │             • 目录深度层级递减
       │                                │             • 噪声目录强制压底
       └────────────────────────┬───────┴────────────────────────┘
                                │
                                ▼
                     综合总分降序重排 (TotalScore)
                                │
                                ▼
                   截取 Top N 提交 Slint UI 渲染
                                │
                   用户回车激活 / 打开所在目录
                                │
                                ▼ (异步持久化)
                   HistoryManager (%APPDATA%/ZenSearch/history.tsv)
```

#### 1. 评分维度与算法公式
每个候选结果的最终排序得分由四个维度复合计算：
$$\text{TotalScore} = S_{\text{match}} + S_{\text{history}} + S_{\text{type}} + S_{\text{path}}$$

* **文本匹配分 ($S_{\text{match}}$，基础核心权重)**：
  * **完全全字匹配（Exact Match）**：文件名或主干去除扩展名后完全相符，给予最高基础分（`+12,000`）；
  * **前缀匹配（Prefix Match）**：文件名以搜索词开头（`+6,000`），主干以搜索词开头（`+5,500`）；
  * **连续子串匹配（Substring Match）**：连续出现在文件名中，并根据命中位置越靠近首部给予递增加成（`+1,000 ~ +3,000`）；
  * **缩写/首字母匹配（Acronym Match）**：支持单词首字母与 CamelCase 驼峰匹配（如 `vsc` 命中 `Visual Studio Code.exe`，`+4,500`）；
  * **紧凑度惩罚**：文件名相比搜索词过长时进行微量扣分，保证匹配越紧凑排名越前；
  * **纯父路径命中惩罚**：若搜索词仅命中父目录而未命中文件名，得分大幅衰减（仅 `+300`）。

* **自适应学习与使用习惯分 ($S_{\text{history}}$，动态演进权重)**：
  * **Query 记忆绑定（Query-Specific Boost）**：当用户曾输入该特定 Query 并回车启动过某文件时，直接赋予极高专项加成（`+15,000`），达成肌肉记忆秒开；
  * **总打开频次（Frequency）**：采用对数递增模型 $\log_2(\text{launch\_count} + 1) \times 600$，防止高频项长期永久压制；
  * **时间新鲜度（Recency 时间半衰期衰减）**：
    $$\text{Score}_{\text{recency}} = 3000 \times 2^{-\frac{\Delta t}{T_{\text{half}}}}$$
    其中半衰期设定为 3 天（$T_{\text{half}} = 259,200\text{s}$）。刚访问过的文件获得前列置顶，随着时间推移自然平滑回归基准线。

* **场景上下文与类型分 ($S_{\text{type}}$)**：
  * **主启动器上下文（`SearchContext::Launcher`）**：`.exe`、`.lnk`、`.bat`、`.cmd`、`.appref-ms` 等可执行程序获得优先加分（`+2,000`）；
  * **资源管理器随行上下文（`SearchContext::Companion`）**：目录/文件夹获得专属加分（`+2,500`），无缝满足资源管理器穿梭跳转需求。

* **路径深度与噪声目录抑制 ($S_{\text{path}}$)**：
  * **深度层级惩罚**：路径每多一级目录惩罚 40 分，避免深层无关嵌套项刷屏；
  * **噪声目录强力打压**：针对 `node_modules`、`.git`、`target\debug`、`target\release`、`AppData\Local\Temp`、`$Recycle.Bin` 等编译中间件或缓存路径，强行扣除 `6,000` 分沉底。

#### 2. 接口实现规范 (`src/core/everything.rs` & `src/core/ranking/`)

```rust
pub fn execute_ranked_query(
    query: &str,
    max_results: u32,
    context: SearchContext,
) -> QueryOutput {
    let trimmed = query.trim();
    if trimmed.is_empty() {
        return QueryOutput::default();
    }

    // 1. 宽进粗排：向 Everything 底层拉取 100~300 条候选池
    let candidate_pool_size = (max_results * 10).max(100).min(300);
    let raw_output = execute_query(query, candidate_pool_size);

    if raw_output.results.is_empty() {
        return raw_output;
    }

    // 2. 精排计算：调用 Ranker 综合评分重排
    let history = HistoryManager::global();
    let ranked = Ranker::rank(raw_output.results, trimmed, context, &history);

    // 3. 截取 Top N 提交渲染
    let final_results: Vec<SearchResult> = ranked.into_iter().take(max_results as usize).collect();

    QueryOutput {
        results: final_results,
        total_matched: raw_output.total_matched,
    }
}
```

#### 3. 用户交互正向反馈机制
* 当用户在浮窗中按下回车激活项（`on_item_activated`）或右键直接定位所在目录（`on_item_open_folder`）时，自动触发：
  ```rust
  core::ranking::HistoryManager::global().record_launch(&query, &path);
  ```
* 记录原子化更新至单例状态，并实时异步持久化至用户目录 `%APPDATA%\ZenSearch\history.tsv`，保证下次同类查询立即可用。


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
                // 向 UI 线程发送无阻塞唤醒信号
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

### 3. 双击 Ctrl 穿透前台锁与输入框原子聚焦 (Direct Focus Pipeline)
> [!IMPORTANT]
> **前台锁穿透（Foreground Lockout Bypass）**：Windows 操作系统具备严格的前台焦点限制保护。ZenSearch 采用跨进程输入挂载与激活管道，确保浮窗呼出瞬间系统键盘焦点 100% 直达搜索输入框，杜绝丢字与光标失焦。

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
                                            [ 步骤 7: 跨进程前台锁穿透置顶 ]
                                            AttachThreadInput + SetForegroundWindow
                                                      │
                                                      ▼
                                            [ 步骤 8: 原子级光标聚焦搜索栏 ]
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
* **可执行程序（.exe/.lnk）**：精准提取呈现内嵌专属 LOGO 图标；
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
  * 针对 `.exe`、`.lnk` 等具备独立内嵌图标的程序，按绝对路径哈希并限制上限 200 项，防止内存无序增长；
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
