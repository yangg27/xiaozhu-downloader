# 小助下载器 v2.5.2 — 修复 CI 构建脚本 + 全功能复测通过

## 下载

- **Windows 安装包**：`小助下载器_v2.5.2_安装包.exe`（32.0 MB）
- SHA256：`8e3ab0bef6aa54bcc90a8c6be45a6f16f03a8079c07aa5e5a3576b79ba99bc22`
- 安装后无需单独安装 aria2c，双击即用。

## v2.5.2 修复与改进

### 1. 修复 CI 构建脚本（关键）

- **问题**：`_t/build_app.py` / `_t/build_setup.py` 中硬编码了 `C:\Users\Sunny_yang\...` 等本地 Python 路径，
  在 GitHub Actions `windows-latest` runner 上找不到，导致推送 `v*` tag 后构建失败。
- **修复**：移除硬编码路径，改为优先读取 `PYTHON` 环境变量，其次 `shutil.which("python")`，
  最后回退到 `sys.executable`。

### 2. 保留 v2.5.1 全部功能

- aria2c 备用极速通道（HTTP 大文件走 aria2c，HLS/DASH 走 N_m3u8DL-RE）。
- 修复 PyInstaller one-dir 模式下 `_internal/third_party/aria2/` 路径探测。
- GitHub Actions 自动发布工作流。

### 3. 版本号统一升级到 2.5.2

- `dlcore.py`、`gui.py` 引擎版本统一为 `2.5.2`。
- `_t/build_setup.py` 安装包版本改为 `2.5.2`，产物文件名同步更新。
- `README.md` 下载链接同步更新。
- `使用说明.md` 新增 v2.5.2 章节。

## 全功能复测摘要（v2.5.2）

| 测试项 | 结果 |
|---|---|
| 源码 AST 语法检查 | 通过 |
| 主程序构建（PyInstaller one-dir） | 通过 |
| 安装包构建 | 通过 → 32.0 MB |
| 安装包 payload 文件数 | 969 个文件 |
| 打包后 aria2c 可被找到 | 通过（`_internal/third_party/aria2`） |
| aria2c HTTP 下载 | 通过（12.95 KiB，1.18s） |
| N_m3u8DL-RE HLS 下载 | 通过（465.59 MiB，25.08s） |
| dlcore ERROR → aria2c → DONE | 通过 |
| GUI 工具栏/右键菜单绑定 | 通过 |
| CLI `--help` / `--json` 下载 | 通过 |
| 断点续传（本地服务器） | 通过 |
| 错误提示含 aria2c 引导 | 通过 |
| GitHub Actions workflow 结构 | 通过 |

## 完整变更

详见 `v2.5.2` 标签及之后提交。
