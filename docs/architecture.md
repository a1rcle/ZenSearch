# ZenSearch 全局系统架构与规范指南 (Architecture & Global Standards)

> **文档版本**：v2.0 (系统设计快照)  
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

ZenSearch 是一款面向 Windows 平台的高性能桌面启动与搜索增强工具（类似 Listary）。系统采用 **Rust 原生语言全栈构建**，实现零成本抽象、无 GC 垃圾回收停顿、冷启动时间 $\le 10\text{ms}$、常驻内存仅 **8~15MB**。

### 1. 核心技术栈矩阵

| 维度 | 选定方案 | 选型考量与优势 |
| :--- | :--- | :--- |
| **编程语言** | **Rust (edition 2021, MSVC 编译目标)** | 零成本抽象、无 GC 垃圾回收停顿、编译期保证线程并发安全、常驻内存仅 **8~15MB**。 |
| **Win32 系统绑定** | **微软官方 `windows` crate (v0.58+)** | 微软官方维护，提供对 Win32 API、COM 接口、UI Automation、注册表和内核对象的 100% 强类型原生支持。 |
| **GUI 渲染框架** | **Slint (v1.8+)** | 专为桌面端设计的高性能现代 GUI 框架；声明式 DSL 语法、原生支持 Per-Monitor 高 DPI、内置 GPU/软件渲染器，启动极速。 |
| **底层搜索索引** | **Everything-SDK (C FFI) + 随包内置 Everything** | 毫秒级内存索引；安装包直接捆绑绿色版 `Everything.exe`，实现开箱即用的系统级极速搜索。 |
| **日志与诊断** | **`tracing` + `tracing-appender`** | 异步非阻塞日志框架，毫秒级时间戳，自动按大小与日期滚动切分。 |
| **安装与交付** | **Inno Setup (v6.3+)** | 标准 Windows 安装器，支持自动退出运行中进程、配置计划任务最高特权自启及干净卸载。 |

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
|  [ 2. 搜索服务与排序引擎中台 (Search & Ranking Engine) ]                             |
|       ├── Everything-SDK 原生 FFI 绑定与状态机复位管道                                 |
|       ├── 宽进精排架构 (Everything 粗排 100~300 条候选 + Rust 纳秒级精排)              |
|       ├── 综合多维打分引擎 (全字/前缀/子串/缩写/深度惩罚/脏目录抑制)                   |
|       ├── 自适应使用学习与 Query 记忆绑定 (半衰期指数时间衰减模型)                      |
|       ├── 场景上下文感知 (Launcher 可执行文件优先 / Companion 目录优先)                |
|       ├── 随包捆绑 Everything 进程守护与静默拉起器 (-startup)                         |
|       ├── Win32 Shell 原生图标提取与零 I/O 虚拟属性两级常驻缓存 (icon_provider)        |
|       └── Scoped Search 语法装配器与流式分发管道                                       |
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

### 2. Rust 工程目录结构

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
│   ├── main.slint                # 界面导出聚合入口
│   └── common.slint              # 调色板、圆角与矢量图标组件
├── src/
│   ├── main.rs                   # 程序入口、单实例互斥与事件循环驱动
│   ├── lib.rs                    # 核心库导出与集成测试入口
│   ├── core/                     # 系统核心服务
│   │   ├── autostart.rs          # 计划任务与注册表自启动管理
│   │   ├── everything.rs         # Everything-SDK FFI 接口与守护进程拉起
│   │   ├── explorer.rs           # IShellWindows COM 互操作、路径提取与独立窗口定位
│   │   ├── hotkey.rs             # WH_KEYBOARD_LL 键盘钩子与双击状态机
│   │   ├── icon_provider.rs      # Win32 Shell 原生图标提取与零 I/O 双级缓存
│   │   ├── injector.rs           # #32770 对话框路径注入与回环校验
│   │   ├── ranking/              # Listary 式综合评分排序与历史自适应引擎
│   │   │   ├── mod.rs            # 模块对外统一接口导出
│   │   │   ├── scorer.rs         # 多维复合打分算法 (匹配度/上下文/深度惩罚)
│   │   │   └── history.rs        # 启动历史、使用频次、半衰期衰减与 Query 绑定存储
│   │   ├── scale_manager.rs      # DPI 感知与动态尺寸推导
│   │   ├── single_instance.rs    # Win32 命名互斥体与已有实例激活
│   │   └── window_helper.rs      # 窗口句柄查找、Z-Order 提升与前台聚焦辅助
│   ├── host/                     # 隐藏宿主窗口与托盘
│   │   ├── tray.rs               # Windows 原生托盘图标与右键菜单
│   │   └── message_host.rs       # 隐藏 Win32 消息窗口 (接收二次启动唤醒指令)
│   └── utils/
│       ├── logger.rs             # tracing 滚动日志初始化
│       └── paths.rs              # 标准目录解析 (%APPDATA%, %LOCALAPPDATA%)
├── tests/                        # 集成与自动化测试套件
│   ├── test_path_injection.rs    # 对话框路径注入模拟集成测试
│   ├── test_ranking_algorithm.rs # 综合排序算法与自适应学习单元测试
│   ├── test_search_query.rs      # Everything 语义与 Deref 规范测试
│   └── test_ui_scale.rs          # 高 DPI 与窗口缩放推导单元测试
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

ZenSearch 严格遵循 Windows 平台的特权模型与目录写入规范，保障多用户及企业权限环境下的系统稳定与安全。

### 1. 标准存储目录规范（读写分离设计）
程序安装于 `C:\Program Files\ZenSearch` 或用户目录时，实行严格的读写分离：
* **只读应用目录 (`%ProgramFiles%\ZenSearch` 或 `%LOCALAPPDATA%\Programs\ZenSearch`)**：
  * 存放 `zensearch.exe`、`Everything64.dll`、`tools/Everything.exe` 以及预设静态资源，仅允许安装/升级程序写入。
* **用户配置目录 (`%APPDATA%\ZenSearch`)**：
  * 存放用户配置文件 `config.toml`。
* **日志与临时缓存目录 (`%LOCALAPPDATA%\ZenSearch\logs`)**：
  * 存放循环滚动日志 `zensearch.log`。

### 2. UIPI 特权穿透与双通道自启体系 (Dual-Channel Autostart & UIPI Bypass)
* **运行机制与双通道协同**：
  * **计划任务特权自启通道**：Inno Setup 安装包在管理员安装模式下，通过 Windows **任务计划程序 (Task Scheduler)** 注册自启动任务，设定属性 `RunLevel: HighestAvailable`，在用户登录时以当前用户最高可用权限静默启动 `zensearch.exe --silent`；
  * **注册表常规自启通道**：普通安装或用户在托盘菜单直接切换自启时，配置 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`；
* **极速无感探测与控制台隔离**：
  * 状态检测遵循“纯注册表首检短路 + 计划任务状态 60s TTL 内存缓存 + 启动期后台异步预热”设计，将菜单呼出前置耗时压降至 $< 0.05\text{ms}$，杜绝主线程阻塞与顿卡；
  * 所有针对 Windows 计划任务的命令行调用强制配置 `CREATE_NO_WINDOW (0x08000000)` 创建标志，杜绝临时控制台黑框窗口闪烁；
* **架构收益**：
  * ZenSearch 开机自动以高权限常驻，**无需触发 UAC 弹窗**，即可穿透 UIPI（用户界面特权隔离），向以管理员权限运行的软件（如专业 IDE、系统工具）所调起的文件对话框提供无缝路径注入。

### 3. Everything 零配置随包内置
* 安装程序将绿色免安装版 `Everything.exe` 部署至应用子目录 `tools/`；
* 启动时四级探针检测到系统中未运行 Everything 服务时，自动静默拉起 `tools/Everything.exe -startup`；
* 达成用户安装即用、底层索引毫秒级响应的零额外配置体验。

---

## 四、 单实例运行与已有实例激活规范 (Single Instance Management)

### 1. Win32 内核命名互斥体
* 使用 `windows::Win32::System::Threading::CreateMutexW` 创建命名互斥体：
  `Local\ZenSearch_SingleInstance_Mutex_{USERNAME}`。
* **首行拦截原则**：在 `main.rs` 入口首行尝试 `acquire()`。若 `GetLastError() == ERROR_ALREADY_EXISTS`，判定已有实例在运行并立即流转至激活流程。

### 2. 已有实例前台激活时序
```rust
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

## 五、 核心关键机制与技术设计 (Core Mechanisms)

| 核心维度 | 关键设计机制 | 架构实现要点 | 模块文档 |
| :--- | :--- | :--- | :--- |
| **低级键盘钩子 (Low-Level Hook)** | 零堆分配与异步抛出通道 | 钩子回调函数内部零堆分配、零磁盘/网络 I/O，使用原子时间戳判定，通过无锁通道（Channel）将事件抛至后台线程处理，确保耗时远低于系统超时阈值。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **多屏混合 DPI 自适应** | Per-Monitor V2 声明与逻辑像素推导 | 显式声明 `Per-Monitor V2` DPI 感知；基于 Slint 逻辑像素自动缩放系统，监听 `WM_DPICHANGED` 并根据前台窗口/光标所在显示器动态适配。 | [responsive_ui.md](file:///d:/ZenSearch/docs/features/responsive_ui.md) |
| **焦点栈追踪 (Focus Stack)** | 资源管理器活动窗口精准判定 | 通过 `SetWinEventHook` 维护前台焦点栈，避免无序枚举，精准锁定用户交互的上一个活动 Explorer 窗口。 | [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) |
| **对话框路径注入 (Quick Switch)** | 物理释放等待 + 焦点强锁 + 回环校验 | 等待鼠标物理左键（`VK_LBUTTON`）释放以完成点击结算，使用 `AttachThreadInput` + `SetFocus` 强行锁定 Edit 焦点，配合地址栏回环校验与最多 3 次重试自愈机制。 | [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) |
| **UIPI 特权隔离穿透** | 计划任务高特权自启 | 安装期配置 `RunLevel: HighestAvailable` 计划任务，开机免 UAC 弹窗以管理员权限常驻，穿透 UIPI 向提权进程注入路径。 | 本文档 第三章 |
| **系统托盘与自启轻量探测** | 注册表首检短路 + 计划任务 TTL 缓存 + `CREATE_NO_WINDOW` | 托盘右键呼出菜单时优先读取注册表并命中内存原子缓存，主线程无子进程阻塞延迟；所有外部命令强制加 `CREATE_NO_WINDOW` 隔离控制台黑框；`TrackPopupMenu` 后追加 `WM_NULL` 保证外部失焦即关。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **浮窗无状态重置 (Clean Slate)** | 唤醒即时重置管道 | 每次唤醒浮窗时强制清空输入框文本、清空候选模型、重置查询 Token、视口与选中索引归零，彻底隔绝前次状态残留。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **轻量浮窗与失焦收起** | `WS_EX_TOOLWINDOW` 与失焦即隐 | 设置 `WS_EX_TOOLWINDOW` 剥离任务栏占位；监听 `WM_ACTIVATE (WA_INACTIVE)` 及 `WM_KILLFOCUS` 实现点击外部区域即时无感隐去。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **双击 Ctrl 焦点穿透** | OS 前台锁穿透与输入框聚焦 | 调用 `AttachThreadInput` + `SetForegroundWindow` + `SetFocus` 穿透前台锁，联动 Slint `invoke_focus_input` 确保光标立即可键入。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **候选列表视口滚动跟随** | `scroll_y` 双向绑定与最小滚动算法 | 顶层 `scroll_y` 属性与 `ListView` 视口 `content-y` 双向绑定；通过 `ensure_selected_visible()` 算法在光标移动时计算最小视口偏移，支持首末项边界驻留。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md)<br>[explorer_companion.md](file:///d:/ZenSearch/docs/features/explorer_companion.md) |
| **独立窗口安全定位** | 原生命令行直通与路径清洗 | 采用 `CommandExt::raw_arg` 传递原生无转义参数；清洗路径结尾多余反斜杠并保留根目录驱动器标识；加入 `/n` 参数强制开启独立新 Explorer 窗口。 | [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) |
| **Windows 11 多标签页适配** | UI Automation 树遍历与 COM 对齐 | 使用 UI Automation 树遍历定位当前激活标签页，并与 `IShellWindows` 实例精确对齐，实现多 Tab 场景下的准确路径提取。 | [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) |

---

## 六、 项目构建与编译发布指南 (Build & Compilation Guide)

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

> **说明**：代码库中已提交预编译好的 `assets/zensearch.res`。日常 Rust 源码修改无需重复执行上述命令，`build.rs` 会自动将该 `.res` 传入 MSVC 链接器。

### 3. 日常开发与测试编译 (Development & Test)

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
* `build.rs` 在构建完成后自动将 `tools/Everything64.dll` 拷贝至 `target/debug/` 与 `target/debug/deps/`，确保调试运行直接加载动态库。

### 4. 正式发行版编译 (Release Build)

发布前执行全面编译优化：

```powershell
# 编译高度优化的生产环境 Release 二进制
cargo build --release
```

* **构建产物路径**：`target/release/zensearch.exe`
* **产物特性**：
  * 静态嵌入 Slint 编译态代码，消除运行时解释开销；
  * 内置 16x16 至 256x256 多尺寸高清图标；
  * 执行文件体积约 15~16MB，常驻内存仅约 8~15MB。

### 5. 安装包打包 (Installer Packaging)

使用 Inno Setup 6 编译生成独立安装程序：

```powershell
# 调用 Inno Setup 命令行编译器 (需已安装 Inno Setup 6)
ISCC.exe installer\zensearch.iss
```

* **安装包产物路径**：`target/installer/ZenSearch_Setup_v2.0.0.exe`
* **打包特性**：
  * 打包 `zensearch.exe`、`Everything64.dll` 以及随包附带的 `tools/Everything.exe`；
  * 自带定制安装向导图标；
  * 支持静默创建 Windows 任务计划程序高特权开机自启项。
