# ZenSearch 2.0

> **极速、静默、优雅的 Windows 搜索与资源管理器路径增强伴侣**

ZenSearch 是专为 Windows 打造的系统级快速文件搜索与工作流增强工具。采用原生 **Rust + Slint UI** 构建，底层直接调用 **Everything C FFI 原生引擎**，具备微秒级响应、超低内存占用以及平滑的高 DPI 适配支持。

---

## ✨ 核心特性

- ⚡ **原生极致性能**：基于 Rust 重构，告别解释型脚本与臃肿 Web 框架，启动迅速、内存极低。
- 🔍 **Everything 原生 FFI 直连**：直接通过 FFI 绑定 Everything IPC 通信，毫秒级检索数百万文件。
- 🎯 **全局快捷唤醒 (Alt+Space)**：一键呼出搜索框，支持键盘快捷键导航与直接回车定位或执行。
- 📂 **文件资源管理器伴侣 (Explorer Companion)**：实时侦测当前活动的资源管理器窗口，按下快捷键（默认 `Alt+F`）直接将选中文件路径注入或开启伴侣导航。
- 🎨 **现代化流畅界面**：基于 Slint GUI 引擎构建，完美支持 Windows 多显示器异构 DPI 动态缩放。
- 🛡️ **高权限与静默托盘运行**：支持开机自启、托盘后台驻留、平滑进程互斥防多开。

---

## 🛠️ 技术架构

- **核心语言**：Rust 2021 Edition
- **UI 框架**：[Slint UI](https://slint.dev/) (1.8+)
- **Windows API**：`windows` crate (0.58)
- **底层搜索引擎**：Everything SDK 64-bit C API
- **打包安装**：Inno Setup (`installer/zensearch.iss`)

更多架构与内部设计请参阅 [docs/architecture.md](docs/architecture.md)。

---

## 🚀 快速开始

### 前置要求

- 操作系统：Windows 10 / 11 (x64)
- Rust 工具链：`rustc` & `cargo` (1.75+)
- [Everything](https://www.voidtools.com/) 搜索服务（项目内置支持静默守护）

### 本地编译运行

```powershell
# 1. 克隆代码仓库
git clone https://github.com/a1rcle/ZenSearch.git
cd ZenSearch

# 2. 调试运行
cargo run

# 3. 生产发布构建
cargo build --release
```

编译产物将生成在 `target/release/zensearch.exe`。

### 运行测试

```powershell
cargo test
```

---

## 📦 打包安装程序

项目提供了基于 Inno Setup 的安装向导脚本：

1. 确保已运行 `cargo build --release` 生成 release 可执行程序。
2. 使用 Inno Setup 编译器打开 `installer/zensearch.iss` 并点击 **Compile**。
3. 安装包将输出至 `target/installer/ZenSearch_Setup_v2.0.0.exe`。

---

## 📄 开源许可证

本项目遵循 MIT 许可证，详情请查看 LICENSE 文件。
