# 小助下载器 v2.5.3 — 全功能复测通过（未发布到 GitHub）

## 下载

- **Windows 安装包**：`小助下载器_v2.5.3_安装包.exe`（约 32 MB）
- 安装包已构建完成：`小助下载器_v2.5.3_安装包.exe`（32.0 MB，SHA256 `5cb8a09fd396a704934e6e76f58fa2b1ccd154e9cb9c187094ec81e742825922`）
- 安装后无需单独安装 aria2c，双击即用。
- **注意**：用户要求取消 GitHub 发布，v2.5.3 tag 仅存在于本地，未 push。

## v2.5.3 修复与改进

### 1. 版本号统一升级到 2.5.3

- `dlcore.py`、`gui.py` 引擎版本统一为 `2.5.3`。
- `_t/build_setup.py` 安装包版本改为 `2.5.3`，产物文件名同步更新。
- `README.md`、`xzd_app.spec` 同步更新。

### 2. 保留 v2.5.2 全部功能

- aria2c 备用极速通道（HTTP 大文件走 aria2c，HLS/DASH 走 N_m3u8DL-RE）。
- 修复 PyInstaller one-dir 模式下 `_internal/third_party/aria2/` 路径探测。
- GitHub Actions 自动发布工作流。

## 全功能复测摘要（v2.5.3）

| 测试项 | 结果 |
|---|---|
| 源码 AST 语法检查 | 通过 |
| 主程序构建（PyInstaller one-dir） | 通过 |
| 安装包构建 | 通过 |
| 安装包 payload 文件数 | 969 个文件 |
| 打包后 aria2c 可被找到 | 通过（`_internal/third_party/aria2`） |
| aria2c HTTP 下载 | 通过 |
| N_m3u8DL-RE HLS 下载 | 通过 |
| dlcore ERROR → aria2c → DONE | 通过 |
| GUI 工具栏/右键菜单绑定 | 通过 |
| CLI `--help` / `--json` 下载 | 通过 |
| 断点续传（本地服务器） | 通过 |
| 错误提示含 aria2c 引导 | 通过 |
| GitHub Actions workflow 结构 | 通过 |

## 完整变更

详见 `v2.5.3` 标签及之后提交。
