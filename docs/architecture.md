# ZenSearch 全局系统架构与规范指南 (Architecture & Global Standards)

> **文档版本**：v2.0 (Rust 原生重构版)  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + 微软官方 `windows` crate + Slint 声明式 GUI + Inno Setup 打包器  
> **交付形态**：企业级标准 Windows 安装包（内置 Everything 绿色服务，支持免提权与管理员双模式安装）  
> **模块文档索引**：
> - 极速搜索与主浮窗：[search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md)
> - 路径注入核心引擎：[path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md)
> - 资源管理器随行增强：[explorer_companion.md](file:///d:/ZenSearch/docs/features/explorer_companion.md)
> - 响应式与高 DPI 适配：[responsive_ui.md](file:///d:/ZenSearch/docs/features/responsive_ui.md)
> - 诊断与故障排查手册：[testing_and_debugging.md](file:///d:/ZenSearch/docs/testing_and_debugging.md)

---

## 一、 项目定位与技术选型决议

ZenSearch 是一款面向 Windows 平台的高性能桌面启动与搜索增强工具（类似 Listary）。为了彻底消除 Python 方案在高频键盘低级钩子下的 GC 停顿风险、冷启动慢及高内存占用缺陷，系统采用 **Rust 原生语言全栈构建**。

### 1. 核心技术栈矩阵

| 维度 | 选定方案 | 选型考量与优势 |
| :--- | :--- | :--- |
| **编程语言** | **Rust (edition 2021, MSVC 编译目标)** | 零成本抽象、无 GC 垃圾回收停顿、编译期保证线程并发安全、常驻内存仅 **8~15MB**。 |
| **Win32 系统绑定** | **微软官方 `windows` crate (v0.58+)** | 微软官方维护，提供对 Win32 API、COM 接口、UI Automation、注册表和内核对象的 100% 强类型原生支持。 |
| **GUI 渲染框架** | **Slint (v1.8+)** | 专为桌面端与嵌入式设计的高性能现代 GUI 框架；声明式 DSL 语法、原生支持 Per-Monitor 高 DPI、内置 GPU/软件渲染器，启动耗时 $\le 10\text{ms}$。 |
| **底层搜索索引** | **Everything-SDK (C FFI) + 随包内置 Everything** | 毫秒级内存索引；安装包直接捆绑绿色版 `Everything.exe`，彻底消除要求用户手动去官网下载配置的割裂感。 |
| **日志与回溯** | **`tracing` + `tracing-appender`** | 异步非阻塞日志框架，毫秒级时间戳，自动按大小与日期滚动切分。 |
| **安装与交付** | **Inno Setup (v6.3+)** | 业界成熟的 Windows 安装器，支持安装前自动关闭旧进程、配置计划任务特权自启及干净卸载。 |

---

## 二、 系统架构总览与工程拓扑

### 1. 系统分层架构图

```
+---------------------------------------------------------------------------------------+
|                                  ZenSearch 系统架构 (Rust)                            |
+---------------------------------------------------------------------------------------+
|  [ 1. 全局输入与按键中枢 (Input & Hook Hub) ]                                          |
|       ├── SetWindowsHookExW (WH_KEYBOARD_LL) 零分配无锁钩子                           |
|       ├── 双击 Ctrl 状态机 (Atomic 时间戳与防误触重置)                                |
|       └── 跨线程无阻塞分发通道 (crossbeam-channel / std::sync::mpsc)                   |
+---------------------------------------------------------------------------------------+
|  [ 2. 搜索服务中台 (Search Engine) ]                                                  |
|       ├── Everything-SDK 原生 FFI 绑定 (extern "system" IPC 通道)                      |
|       ├── 随包捆绑 Everything 进程守护与静默拉起器 (-startup)                         |
|       ├── Win32 Shell 原生图标提取与零 I/O 虚拟属性两级常驻缓存 (icon_provider)        |
|       └── Scoped Search 语法装配器与拼音首字母检索重排                                 |
+---------------------------------------------------------------------------------------+
|  [ 3. 主搜索浮窗与无状态唤醒中枢 (Slint Global Launcher) ]                             |
|       ├── 纯浮窗形态与任务栏隐形 (WS_EX_TOOLWINDOW 剥离 WS_EX_APPWINDOW)              |
|       ├── 失焦即隐机制 (Auto-Dismiss on Unfocus: WM_ACTIVATE WA_INACTIVE)              |
|       ├── 双击 Ctrl 前台锁穿透与搜索栏原子聚焦 (AttachThreadInput + SetFocus)         |
|       ├── 唤醒即时重置管道 (输入清空 / 候选置空 / 视口归顶 / 未决查询 Token 熔断)      |
|       ├── 候选列表视口双向绑定与键盘光标滚动跟随 (content-y <=> scroll_y 最小滚动)     |
|       └── 紧凑单行基准折叠算法与 Slint 声明式动画展开                                  |
+---------------------------------------------------------------------------------------+
|  [ 4. 资源管理器随行伴侣 (Explorer Companion) ]                                       |
|       ├── SetWinEventHook (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_LOCATIONCHANGE)       |
|       ├── IShellWindows COM 安全封装 (RAII 套间初始化与活动路径提取)                  |
|       ├── 随行条贴边吸附与结果置顶向上展开布局 (Bottom-Pinned & Top-Anchored Results)  |
|       ├── 随行候选列表键盘光标自动滚动跟随与首末项边界驻留                             |
|       ├── GetGUIThreadInfo 模态菜单状态感知 (右键菜单/系统菜单单键加速键无感放行)      |
|       └── 失焦无感隐去与生命周期管理 (WM_ACTIVATE WA_INACTIVE + 200ms 防抖)             |
+---------------------------------------------------------------------------------------+
|  [ 5. 路径注入核心引擎 (Quick Switch) ]                                               |
|       ├── 焦点转移状态机 (Focus Transition Guard: Explorer -> #32770 对话框)          |
|       ├── 对话框标准控件解析 (EnumChildWindows 过滤搜索框与地址栏 41477)              |
|       ├── 全域点击兼容管道 (VK_LBUTTON 物理释放等待 + AttachThreadInput 强锁 Edit 焦点)|
|       ├── 原生 VK_RETURN 驱动 + 地址栏 ToolbarWindow32 回环自愈校验 (最多 3 次重试)   |
|       └── 前置文件名备份、防篡改过滤与无损还原                                        |
+---------------------------------------------------------------------------------------+
|  [ 6. 响应式与多显示器 DPI 引擎 (Responsive UI Engine) ]                              |
|       ├── SetProcessDpiAwarenessContext (Per-Monitor V2 声明)                         |
|       ├── 目标屏幕嗅探器 (鼠标光标 / 前台活动窗口屏幕)                                |
|       └── Slint 逻辑像素自动缩放与黄金视线居中推导                                    |
+---------------------------------------------------------------------------------------+
|  [ 7. 宿主生命周期与安装集成 (Host Lifecycle & Delivery) ]                             |
|       ├── Win32 命名互斥体单实例守护 (CreateMutexW)                                   |
|       ├── 隐藏 Win32 消息窗口已有实例唤醒通道 (RegisterWindowMessageW)                |
|       ├── UIPI 特权穿透方案 (Windows 任务计划程序免 UAC 开机自启)                     |
|       └── Inno Setup 标准安装、升级与干净卸载流水线                                   |
+---------------------------------------------------------------------------------------+
```

### 2. Rust 工程目录结构规划

```text
ZenSearch/
├── Cargo.toml                    # Rust 依赖与构建元数据
├── build.rs                      # 构建脚本 (编译 Slint UI 模板、链接 Windows 资源与动态库)
├── assets/                       # 应用程序图标与 Windows 资源文件
│   ├── logo.png                  # 高清 PNG 图标源文件 (256x256)
│   ├── logo.ico                  # Windows 多尺寸图标 (16x16 ~ 256x256)
│   ├── zensearch.rc              # Windows PE 资源描述脚本
│   └── zensearch.res             # 预编译 Windows 资源二进制
├── ui/                           # Slint 声明式界面 DSL
│   ├── launcher.slint            # 主搜索浮窗界面设计
│   ├── companion.slint           # 资源管理器随行条界面设计
│   └── common.slint              # 调色板、圆角与矢量图标组件
├── src/
│   ├── main.rs                   # 程序入口、单实例互斥与事件循环驱动
│   ├── core/                     # 系统核心服务
│   │   ├── single_instance.rs    # Win32 命名互斥体与已有实例激活
│   │   ├── autostart.rs          # 计划任务与注册表自启动管理
│   │   ├── everything.rs         # Everything-SDK FFI 接口与守护进程拉起
│   │   ├── explorer.rs           # IShellWindows COM 互操作与当前路径提取
│   │   ├── hotkey.rs             # WH_KEYBOARD_LL 键盘钩子与双击状态机
│   │   ├── icon_provider.rs      # Win32 Shell 原生图标提取与零 I/O 双级缓存
│   │   ├── injector.rs           # #32770 对话框路径注入与回环校验
│   │   └── scale_manager.rs      # DPI 感知与动态尺寸推导
│   ├── host/                     # 隐藏宿主窗口与托盘
│   │   ├── tray.rs               # Windows 原生托盘图标与右键菜单
│   │   └── message_host.rs       # 隐藏 Win32 消息窗口 (接收二次启动唤醒指令)
│   └── utils/
│       ├── logger.rs             # tracing 滚动日志初始化
│       └── paths.rs              # 标准目录解析 (%APPDATA%, %LOCALAPPDATA%)
├── installer/                    # 安装包工程
│   ├── zensearch.iss             # Inno Setup 编译脚本
│   └── assets/                   # 安装器图标、向导图
├── tools/                        # 随包捆绑工具
│   ├── Everything.exe            # 绿色后台版 Everything
│   └── Everything64.dll          # 官方 C FFI 动态库
└── docs/                         # 技术架构与功能规范文档
```

---

## 三、 安装包交付体系与运行规范 (Installer & Delivery)

从便携小工具转型为正式安装包产品，必须遵守 Windows 平台的特权模型与目录写入规范。

### 1. 标准存储目录规范 (消灭 `Access Denied`)
当程序安装在 `C:\Program Files\ZenSearch` 时，标准用户权限严禁写入该目录。ZenSearch 严格实行读写分离：
* **只读应用目录 (`%ProgramFiles%\ZenSearch` 或 `%LOCALAPPDATA%\Programs\ZenSearch`)**：
  * 存放 `zensearch.exe`、`Everything64.dll`、`tools/Everything.exe` 以及预设静态资源。
* **用户配置目录 (`%APPDATA%\ZenSearch`)**：
  * 存放用户配置文件 `config.toml`。
* **日志与临时缓存目录 (`%LOCALAPPDATA%\ZenSearch\logs`)**：
  * 存放循环滚动日志 `zensearch.log`。

### 2. UIPI 特权穿透与任务计划程序免 UAC 自启
* **核心痛点**：若用户以“管理员身份”打开了某些专业软件（如 VS Code、工业软件），普通权限的 ZenSearch 发送 Win32 消息会遭遇 **UIPI（用户界面特权隔离）** 阻断，导致无法向其对话框注入路径。
* **解决方案（行业标杆做法）**：
  * Inno Setup 安装包在管理员安装模式下，利用 Windows **任务计划程序 (Task Scheduler)** 自动创建一个自启动触发器；
  * 设定属性：`RunLevel: HighestAvailable`，在用户登录时运行 `zensearch.exe --silent`；
  * **效果**：ZenSearch 开机自动以高权限静默常驻，**无需弹出任何 UAC 提权确认框**，即可穿透 UIPI 为全系统的提权对话框提供无缝路径注入。

### 3. Everything 零配置随包内置
* 安装包安装时将绿色免安装版 `Everything.exe` 释放至程序子目录 `tools/`；
* 启动时四级探针若检测到系统中未运行 Everything，直接静默拉起自带的 `tools/Everything.exe -startup`；
* 用户安装完 ZenSearch 即可立即使用极速搜索，零额外配置。

---

## 四、 单实例运行与已有实例激活规范 (Single Instance Management)

### 1. Win32 内核命名互斥体
* 使用 `windows::Win32::System::Threading::CreateMutexW` 创建命名互斥体：
  `Local\ZenSearch_SingleInstance_Mutex_{USERNAME}`。
* **首行拦截原则**：在 `main.rs` 的首行即刻尝试 `acquire()`。若 `GetLastError() == ERROR_ALREADY_EXISTS`，立即判定已有实例存在。

### 2. 已有实例前台激活时序 (Rust 实现)
```rust
// 伪代码流程
if !single_instance::acquire() {
    if !args.contains(&"--silent".to_string()) {
        // 1. 查找主实例创建的隐藏宿主窗口
        if let Ok(hwnd) = find_window("ZenSearch_MessageHost") {
            // 2. 突破 Windows 前台锁限制
            unsafe { AllowSetForegroundWindow(ASFW_ANY) };
            // 3. 发送自定义唤醒广播指令
            unsafe { PostMessageW(hwnd, WM_ZEN_WAKEUP, WPARAM(0), LPARAM(0)) };
        }
    }
    std::process::exit(0);
}
```

---

## 五、 全局技术边界与避坑方案矩阵

| 难点 / 边界情况 | 潜在风险 | Rust 架构级解决方案 | 涉及文档 |
| :--- | :--- | :--- | :--- |
| **低级键盘钩子被系统卸载** | 回调耗时超 `LowLevelHooksTimeout`（约 300ms） | 钩子回调零堆分配、零 I/O、原子时间戳比对，通过无锁通道异步抛出事件 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **混合 DPI 跨屏模糊或错位** | 多屏不同缩放比下窗口字体发虚、边距错位 | 声明 Per-Monitor V2；基于 Slint 原生逻辑像素系统，监听 `WM_DPICHANGED` | [responsive_ui.md](file:///d:/ZenSearch/docs/features/responsive_ui.md) |
| **多窗口跳错 (MRU 失真)** | 打开多个 Explorer 时无序命中后台陈旧窗口 | 废除无序 COM 枚举；`SetWinEventHook` 维护前台焦点栈，仅认上一个活动窗口 | [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) |
| **点击对话框空白处或导航栏无法跳转** | `EVENT_SYSTEM_FOREGROUND` 在 `WM_LBUTTONDOWN` 瞬间抢跑，且 `DirectUIHWND` / `SysTreeView32` / `ToolbarWindow32` 抢走键盘焦点导致 `VK_RETURN` 被视图吞没 | 等待物理鼠标左键（`VK_LBUTTON`）松开并完成点击结算，通过 `AttachThreadInput` + `SetFocus(edit_hwnd)` 跨线程强锁文件名编辑框焦点，配合地址栏回环校验与最多 3 次自愈重试 | [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) |
| **提权窗口消息受阻 (UIPI)** | 管理员身份软件的文件对话框拒绝接收路径注入 | Inno Setup 安装包注册最高特权任务计划程序，开机免弹窗高权限自启 | 本文档 第三章 |
| **唤醒残留上次搜索与长列表** | 再次唤醒浮窗时误触发旧文件启动或几何跳跃 | Slint UI 控制器强制执行 Clean Slate 管道：清空模型、折叠高度、置顶聚焦 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **浮窗失焦悬挂或任务栏占位** | 浮窗被当作普通应用常驻任务栏、点击外部不关闭 | 设置 `WS_EX_TOOLWINDOW` 任务栏隐形，捕获 `WM_ACTIVATE (WA_INACTIVE)` / `WM_KILLFOCUS` 自动收起 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **双击 Ctrl 搜索栏丢键/无光标** | Windows 前台限制（Foreground Lockout）阻断 OS 焦点交割 | Win32 `AttachThreadInput` + `SetForegroundWindow` + `SetFocus` + Slint `invoke_focus_input` 穿透流水线 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **键盘上下切换候选时列表不随光标滚动** | Slint `ListView` 位于 `if is_expanded` 条件块内无法跨作用域直接引用，且仅递增/递减 `selected_index` 不改变视口偏移，导致选中高亮移出可视区域 | 顶层声明 `scroll_y` 属性并通过 `content-y <=> root.scroll_y` 与 `ListView` 双向绑定（兼顾鼠标滚轮与键盘联动）；封装 `ensure_selected_visible()` 最小滚动算法（首末项边界驻留不循环），并在搜索词变更与 `reset_state()` 时同步将 `scroll_y` 归零 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md)<br>[explorer_companion.md](file:///d:/ZenSearch/docs/features/explorer_companion.md) |
| **Windows 11 多标签页 (Tabs)** | Win11 单窗口内多 Tab 导致路径抓取错误 | 使用 UI Automation 树遍历当前激活 Tab，并与 `IShellWindows` 句柄精准对齐 | [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) |

---

## 六、 实施路线与任务里程碑清单 (Rust Roadmap)

### 阶段一：Rust 基础脚手架与搜索原型 (MVP)
- [ ] **Task 1.1**：初始化 Cargo 工程，配置 `Cargo.toml`（引入 `windows`、`slint`、`tracing`）。
- [ ] **Task 1.2**：编写 `src/core/everything.rs`，实现 Everything 动态库 C FFI 绑定与基础查询。
- [ ] **Task 1.3**：实现 `src/core/hotkey.rs`，基于 `SetWindowsHookExW` 完成零分配双击 Ctrl 状态机。
- [ ] **Task 1.4**：使用 Slint 实现极速主搜索浮窗，落地**轻量浮窗形态 (WS_EX_TOOLWINDOW)**、**失焦自动隐藏 (Auto-Dismiss)**、**双击 Ctrl 前台穿透直达搜索栏聚焦**、**无状态即时清空规范 (Clean Slate on Wakeup)** 以及**候选列表键盘光标自动滚动跟随 (`ensure_selected_visible`)**。

### 阶段二：资源管理器互操作与路径注入
- [ ] **Task 2.1**：基于 `windows::Win32::System::Com` 封装 `IShellWindows` 安全调用。
- [ ] **Task 2.2**：实现 `#32770` 通用文件对话框捕获模块与控件树解析。
- [ ] **Task 2.3**：实现切回自动跳转（Switch-Back Auto Switch）与 `Ctrl + G` 手动注入，落地全域点击兼容（空白处/导航栏/底栏焦点强锁与鼠标释放等待）、防篡改保护与实机测试 SOP。

### 阶段三：资源管理器随行增强 (Type-to-Search)
- [ ] **Task 3.1**：实现 `SetWinEventHook` 追踪 Explorer 移动与缩放，Slint 随行条贴边吸附与工作区边界碰撞裁剪。
- [ ] **Task 3.2**：实现 Explorer 内焦点与按键过滤白名单，完成直接打字呼出随行条。
- [ ] **Task 3.3**：实现随行条失焦自动隐去（Blur-to-Dismiss）与状态清空（`WM_ACTIVATE WA_INACTIVE` + `EVENT_SYSTEM_FOREGROUND`）。
- [ ] **Task 3.4**：实现搜索结果始终置于随行输入栏上方（逆向向上生长），输入框底部基准保持不动，并支持上下方向键切换候选时列表视口自动滚动跟随。
- [ ] **Task 3.5**：联调 `path:"<CurrentDir>"` 限定搜索与候选激活。

### 阶段四：安装包打包与商业级打磨
- [ ] **Task 4.1**：实现 Win32 命名互斥体 `CreateMutexW` 与已有实例激活通道。
- [ ] **Task 4.2**：编写 Inno Setup 脚本 `installer/zensearch.iss`，完成打包、随包捆绑 Everything 与计划任务特权自启。
- [ ] **Task 4.3**：编写独立自检诊断子命令 `zensearch.exe --diagnose`，完成全链路自动化测试验收。

---

## 七、 项目构建与编译指南 (Build & Compilation Guide)

ZenSearch 采用标准的 Cargo 构建系统，并在 `build.rs` 中集成了 Slint DSL 前端编译、Windows 原生资源绑定与 Everything FFI 链接。

### 1. 构建环境与先决条件 (Prerequisites)

* **操作系统**：Windows 10 / Windows 11 (x64)
* **Rust 编译工具链**：
  * Rust 1.75+ (edition 2021)
  * Target：`x86_64-pc-windows-msvc`
* **C++ 生成工具与 Windows SDK**：
  * Visual Studio 2022 C++ 构建工具或 Visual Studio Community (包含 MSVC `link.exe` 与 Windows 10/11 SDK)
  * 资源编译器 `rc.exe`（通常位于 `C:\Program Files (x86)\Windows Kits\10\bin\<version>\x64\rc.exe`）
* **依赖库资产**：
  * `Everything64.lib`：位于 `Everything-SDK/lib/`（供链接期使用）
  * `Everything64.dll`：位于 `tools/`（供运行期加载，`build.rs` 会自动同步至 `target/debug/` 及 `target/release/`）

### 2. Windows 资源与图标编译 (Resource Pre-compilation)

程序的主图标与 PE 资源定义在 `assets/zensearch.rc` 中。若修改了图标或资源定义，需通过 `rc.exe` 编译生成 `.res` 文件：

```powershell
# 使用 Windows SDK 的 rc.exe 编译资源脚本
& "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\rc.exe" /fo assets\zensearch.res assets\zensearch.rc
```

> **注意**：代码库中已提交预编译好的 `assets/zensearch.res`，日常 Rust 源码修改无需重复执行上述命令。`build.rs` 会自动将该 `.res` 传入 MSVC 链接器，使生成的 `zensearch.exe` 自带多尺寸 Logo 图标。

### 3. 日常开发与调试编译 (Development & Debug)

```powershell
# 1. 语法检查与快速类型检查
cargo check

# 2. 编译 Debug 版本 (包含调试符号与即时增量构建)
cargo build

# 3. 运行完整单元测试与集成测试套件
cargo test

# 4. 直接启动调试运行
cargo run
```

* **构建产物路径**：`target/debug/zensearch.exe`
* `build.rs` 会在构建完成后自动将 `tools/Everything64.dll` 拷贝至 `target/debug/` 与 `target/debug/deps/`，确保直接运行或断点调试时可直接解析动态库。

### 4. 正式发行版编译 (Release Build)

发布前执行全面编译优化：

```powershell
# 编译高度优化的生产环境 Release 二进制
cargo build --release
```

* **构建产物路径**：`target/release/zensearch.exe`
* **产物特性**：
  * 内置多尺寸应用图标（包含 16x16 至 256x256 全套分辨率）；
  * 静态嵌入 Slint 编译态代码，消除运行时解释开销；
  * 执行文件体积约 15~16MB，常驻内存仅约 8~15MB。

### 5. 安装包打包 (Installer Packaging)

ZenSearch 使用 Inno Setup 6 打包为单文件安装程序：

```powershell
# 调用 Inno Setup 命令行编译器 (需已安装 Inno Setup 6)
ISCC.exe installer\zensearch.iss
```

* **安装包产物路径**：`target/installer/ZenSearch_Setup_v2.0.0.exe`
* **打包特性**：
  * 自动打包 `target/release/zensearch.exe`、`tools/Everything64.dll` 以及随包附带的 `tools/Everything.exe`；
  * 安装程序自带定制的 `logo.ico` 图标；
  * 支持静默创建 Windows 任务计划程序高特权开机自启项。

