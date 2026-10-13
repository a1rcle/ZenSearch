# ZenSearch 全链路测试、诊断体系与排错手册 (Testing & Troubleshooting)

> **文档版本**：v2.0 (Rust 原生重构版)  
> **更新时间**：2026-10-13  
> **技术栈选型**：Rust 2021 + `tracing` 异步日志 + Cargo 测试套件  
> **文档定位**：ZenSearch 深度系统集成工具的诊断机制、专项自愈规则与日常排错指南。  
> **关联架构**：[architecture.md](file:///d:/ZenSearch/docs/architecture.md)

---

## 一、 测试机制与调试体系总览

为解决深度系统集成工具在实际运行中容易出现的“按键失效”、“Everything 未响应”、“资源管理器 COM 断开”、“对话框注入不匹配”等隐蔽故障，ZenSearch 建立了基于 Rust 原生生态的**全链路调试与诊断机制**：

```
+---------------------------------------------------------------------------------------+
|                               ZenSearch 调试与测试体系 (Rust)                         |
+---------------------------------------------------------------------------------------+
|  [ 1. 自动化全项体检 ]   ──> zensearch.exe --diagnose (六层健康度扫描: IPC/COM/对话框)|
|  [ 2. 交互式实时监控 ]   ──> zensearch.exe --diagnose-watch (按键时序、焦点类名追踪)  |
|  [ 3. 异步分级日志库 ]   ──> tracing + tracing-appender (%LOCALAPPDATA% 滚动日志)    |
|  [ 4. 自动化回归测试 ]   ──> cargo test --test test_path_injection                   |
+---------------------------------------------------------------------------------------+
```

---

## 二、 自动化全项体检命令 (`--diagnose`)

通过命令行参数直接调起内部自检模块，无需启动完整 UI 即可对各核心模块进行单项校验，输出色彩高亮的健康度评级：

| 诊断层级 | 检查内容 | 判定标准与修复建议 |
| :--- | :--- | :--- |
| **1. 基础环境** | 进程权限、Windows 版本、WindowStation/Desktop 挂载、MSVC 运行库 | 确保进程挂载在 `Default` 桌面会话，具备标准的桌面交互权限。 |
| **2. Everything 探针与连通性** | 运行态 IPC 窗口、系统注册表安装登记、随包内置版路径、IPC 查询吞吐 | 验证四级探针命中情况与查询延迟（<5ms）；若未运行验证静默拉起。 |
| **3. 资源管理器互操作** | `IShellWindows` 实例枚举、有效物理路径抓取、异常标签页隔离 | 枚举所有活动 Explorer 窗口并测试路径转换；防止 `CO_E_OBJNOTCONNECTED` 抛出。 |
| **4. 文件对话框注入** | 当前屏幕 `#32770` 对话框探测、文件名 Edit 控件寻址、可见性校验 | 检测当前活动对话框树状结构，确认是否存在标准 Edit 控件以保证 `WM_SETTEXT` 成功注入。 |
| **5. 开机自启动配置** | 计划任务 `ZenSearch_Startup` 状态、`HKCU\...\Run` 备用键、`--silent` 参数 | 校验自启动路径是否存在，计划任务特权等级是否为 `HighestAvailable`。 |
| **6. 单实例与多进程健康度** | 检查当前系统中是否存在多个 ZenSearch 残留孤儿进程；校验 Named Mutex 锁状态 | 探测是否有残留冲突进程；验证二次执行时能否正确唤醒旧实例并优雅退出。 |

**执行命令**：
```powershell
# 源码环境
cargo run -- --diagnose

# 编译/安装后环境
zensearch.exe --diagnose
```

---

## 三、 交互式实时监控器 (`--diagnose-watch`)

针对“双击 Ctrl 偶尔不触发”或“资源管理器随行打字无反应”的交互故障，提供实时按键与焦点侦听模式：
* **时序精准量化**：实时打印每次按下 Ctrl 的微秒级时间戳差值 $\Delta t$（ms），直观观测是否落在 $50\text{ms} < \Delta t < 350\text{ms}$ 的有效窗口内；
* **组合键防误触跟踪**：实时显示是否有其他按键（如 `Ctrl+C`、`Ctrl+V`）清空了双击计数器；
* **前台焦点与类名追踪**：实时输出当前前台窗口 HWND、Window Class 与 Title，明确随行搜索未弹起时前台类名是否为 `CabinetWClass`，以及光标是否正处于编辑框中。

**执行命令**：
```powershell
cargo run -- --diagnose-watch
```

---

## 四、 异步分级日志体系 (`tracing`)

* **非阻塞异步写入**：采用 `tracing` + `tracing-appender`，日志格式化与文件写入在独立后台线程执行，**绝不阻塞主消息泵与键盘钩子**；
* **标准文件存储位置**：`%LOCALAPPDATA%\ZenSearch\logs\zensearch.log`，单文件上限 10MB，自动维护 5 个历史轮转备份；
* **调试模式**：
  ```powershell
  zensearch.exe --debug
  ```
  在控制台输出彩色 ANSI 高亮 Trace 日志，并包含精确到纳秒级的函数调用跨度（Span）。

---

## 五、 核心模块专项调试断点与自愈机制

### 1. Everything 探针与随包版自愈
* **故障现象**：搜索列表空白，查询返回空。
* **自愈机制**：
  1. 调用探针扫描四级路径；
  2. 若系统未安装，自动以 `tools/Everything.exe -startup` 静默启动随包版并挂载交互桌面；
  3. 轮询 `FindWindowW` 建立 IPC 通道，全程无需弹出全屏窗口。

### 2. 低级键盘钩子 (`WH_KEYBOARD_LL`) 零开销保活
* **防护规则**：
  1. **零堆分配原则**：回调函数内仅操作原子时间戳 `AtomicI64`，执行耗时压降在 **0.01ms** 以内；
  2. **通道异步分发**：事件通过无锁通道发射至 UI 线程；
  3. **环境隔离保护**：在钩子线程启动前强制执行 `SetThreadDesktop` 挂载 `Default` 桌面。

### 3. 路径注入一致性强校验
* 详细技术实现与标准回归流程详见：[path_injection.md - 第五章 路径注入与真实跳转一致性标准测试 SOP](file:///d:/ZenSearch/docs/features/path_injection.md#五-路径注入与真实跳转一致性标准测试-sop-testing-sop)。
* 执行命令：`cargo test --test test_path_injection`。

### 4. 资源管理器随行条失焦隐去与结果置顶布局自愈
* **交互准则**：
  1. **失焦隐去一致性**：随行条呼出后，只要用户点击 Explorer 外部任意位置、切换前台任务，必须在 50ms 内完全隐去并调用 `reset_state`；
  2. **底部不动点校验**：输入关键词触发结果展开时，随行输入框的屏幕底部绝对坐标必须保持不变，结果列表必须向上延展（置于输入框上方），杜绝下溢出屏幕或遮挡任务栏。
* **验收流程**：在资源管理器内打字唤起随行条，输入检索词后校验列表是否向上浮现，随后鼠标点击桌面空白处，确认随行条瞬间消失且后台状态完全重置。

---

## 六、 故障排查手册 (Troubleshooting Playbook)

| 异常现象 | 可能诱因 | 推荐排查与解决步骤 |
| :--- | :--- | :--- |
| **双击 Ctrl 全局搜索框不弹出** | 1. 两次按键间隔不在 50~350ms 之间<br>2. 夹杂了其他非 Ctrl 按键<br>3. 进程未能挂载至活动交互桌面 | 运行 `zensearch.exe --diagnose-watch` 观察控制台输出的毫秒差与状态转移日志，调整按键习惯或检查是否有全局键位拦截软件冲突。 |
| **搜索框输入后无任何结果** | 1. Everything 未在用户会话运行<br>2. 随包版未被成功拉起 | 运行 `zensearch.exe --diagnose` 检查第二项。确保后台存在 `Everything.exe` 进程。 |
| **另存为对话框无法注入路径** | 1. 对话框宿主软件以管理员权限运行（受 UIPI 拦截）<br>2. 非标准 Common Dialog | 检查是否通过 Inno Setup 正式安装；确认计划任务特权自启是否生效。若以普通权限启动，尝试右键“以管理员身份运行”ZenSearch 验证。 |
| **只能点击对话框下方部分触发跳转，点击空白处或导航栏无法跳转** | 1. `EVENT_SYSTEM_FOREGROUND` 在鼠标按下（`WM_LBUTTONDOWN`）瞬间触发，子控件尚未完成点击结算<br>2. 点击空白处（`DirectUIHWND`）或导航栏（`SysTreeView32` / `ToolbarWindow32`）抢走键盘焦点，导致 `comdlg32.dll` 将 `VK_RETURN` 路由给视图并忽略 `Edit` 控件 | 确保 `inject_path_to_dialog` 执行 `wait_for_mouse_release()` 等待物理鼠标释放，并通过 `AttachThreadInput` + `SetFocus(edit_hwnd)` 强制锁定文件名编辑框焦点及地址栏回环重试。运行 `cargo test --test test_path_injection` 验证 `test_live_dialog_blank_and_navbar_focus_injection` 通过。 |
| **cmd 显示成功但 dialog 真实路径不同** | 1. 路径缺失尾部反斜杠被识别为文件<br>2. 未进行地址栏回环真实校验 | 参考 [path_injection.md](file:///d:/ZenSearch/docs/features/path_injection.md) 标准测试 SOP，确保更新至包含 `VK_RETURN` 驱动与 `ToolbarWindow32` 回环校验的最新注入器版本。 |
| **高分屏下浮窗极小/模糊或副屏弹窗偏离** | 1. 未声明 Per-Monitor V2 模式<br>2. 硬编码使用了主屏幕居中 | 确认主程序入口已调用 `init_high_dpi_context()`，并使用 `scale_manager` 进行基于鼠标所在屏幕的动态居中推导。 |
| **后台出现多个 ZenSearch 进程 / 托盘重复图标** | 1. 未启用 Win32 命名互斥体拦截<br>2. 系统中有历史未退出的残留孤儿进程 | 运行 `zensearch.exe --diagnose` 检查第 6 项；清理残留的孤儿进程；确保主程序最前置调用 `single_instance::acquire()`。 |
| **双击 Ctrl 唤醒后仍残留上次搜索内容或列表** | 1. 未在唤醒入口执行 Clean Slate 管道<br>2. 前序异步查询未取消导致迟到回调覆盖 | 确保遵循 [search_launcher.md](file:///d:/ZenSearch/docs/features/search_launcher.md) 唤醒重置管道规范：在唤醒前置阶段强制重置 Slint 状态模型并熔断未决查询 Token。 |
| **随行搜索条失去焦点后滞留未关闭** | 1. 未子类化拦截 `ZenSearch_Companion` 的 `WM_ACTIVATE (WA_INACTIVE)`<br>2. `win_event_proc` 未感知新前台离开 Explorer | 检查是否对 `CompanionWindow` 的 HWND 正确注册消息子类化与前台变动广播，确认失焦隐去事件成功投递。 |
| **随行搜索结果列表向下遮挡任务栏或跳动** | 1. UI 仍使用“输入栏在上、结果在下”的正向布局<br>2. 几何推导未实施底部固定锚定算法 | 检查 `companion.slint` 是否将结果 ListView 置于 LineEdit 上方；校验 `calculate_companion_dock_geometry` 是否采用 `target_y = exp_rect.bottom - comp_h - margin` 的底部基准推导。 |
| **按上下方向键切换候选时搜索结果不随光标滚动** | 1. `ListView` 位于 `if is_expanded` 条件块内，仅修改 `selected_index` 未同步更新视口偏移<br>2. 切换搜索词或重置状态时未将 `scroll_y` 归零 | 检查 `launcher.slint` 与 `companion.slint` 是否绑定 `content-y <=> root.scroll_y` 并在 `UpArrow` / `DownArrow` 时调用 `ensure_selected_visible()`。运行 `cargo test test_launcher_and_companion_scroll_follows_selection` 验证视口跟随与复位逻辑。 |
