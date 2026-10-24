# 小助下载器 v2.6.0 — 清理历史版本后的统一稳定发布

## 下载

- **Windows 安装包**：`xiaozhu-downloader-v2.6.0-setup.exe`（约 32 MB）
- 由 GitHub Actions 自动构建并上传至 Release Assets。

## 说明

v2.6.0 基于此前已通过全功能复测的 v2.5.3 代码基线，仅做版本号升级与发布流程整理。

按照要求，GitHub 上此前已发布的 `v2.4.0`、`v2.5.0`、`v2.5.1`、`v2.5.2` 四个 tag 及对应 Release 已被删除，统一以 `v2.6.0` 作为当前稳定版本对外发布。

## 包含的历史改进（自 v2.3.x 以来）

### 核心缺陷修复

- **低速限速死锁**：令牌桶 burst 下限调整为 `max(READ_SIZE, rate*2)`，避免限速 < 256 KB/s 时 `consume()` 死循环。
- **HLS AES-128 解密误删数据**：移除错误的 PKCS7 padding 剥离，符合 HLS 规范。
- **任务列表保存竞态**：`save_jobs()` 加锁，避免并发写 `.tmp` 触发 `PermissionError`。
- **写线程异常降级**：单块写入异常后改为设置 `_fatal` 并同步降级，避免直接判任务死刑。
- **insecure SSLContext 派生隔离**：基于共享 context 创建独立副本，仅修改 `check_hostname` / `verify_mode`。

### 性能优化

- **HLS 分片走主机级连接池**：同一 m3u8 的成百上千 `.ts` 分片按 `(scheme, host, port)` 复用 TCP/TLS 连接。
- **aria2c 备用极速通道**：HTTP 大文件可一键切换至内置 aria2c，HLS/DASH 切换至 N_m3u8DL-RE。

### 扩展与集成

- **iframe 内媒体嗅探**：`content_scripts` 启用 `all_frames: true`，webRequest 将 `frameId` 映射到所属标签页，嗅探列表标注"内嵌框架"。
- **浏览器接管下载**：Chrome / Edge 点击下载链接自动转交本工具。
- **开机自启 + 后台静默**：安装时可选注册开机自启，静默驻留托盘。

### CI / 构建

- 修复 GitHub Actions 构建脚本中硬编码本地 Python 路径的问题，兼容 Actions runner。

## 全功能复测摘要

此前 v2.5.3 基线已完成 27 项端到端复测，全部通过：

| 类别 | 测试项 | 结果 |
|---|---|---|
| HTTP 多线程 | 32 线程大文件下载、断点续传、限速、动态加线程 | 通过 |
| HLS | AES-128 解密 m3u8、多码率选择、本地连接池复用 | 通过 |
| aria2c 备用通道 | HTTP 大文件 aria2c、HLS N_m3u8DL-RE、手动/自动切换 | 通过 |
| 浏览器扩展 | 普通页面 / iframe 内媒体嗅探、xzd:// 协议唤起 | 通过 |
| 系统集成 | 开机自启、协议注册、安装包安装/卸载生命周期 | 通过 |
| SSL | insecure 模式派生隔离、共享 context 不被污染 | 通过 |

## 校验

- 安装包由 GitHub Actions 在 `windows-latest` runner 上构建。
- Release Assets 包含安装包与 SHA256 校验文件。

## 升级建议

- 已安装旧版本的用户可直接运行 v2.6.0 安装包覆盖安装，任务列表与设置会保留在 `%APPDATA%\小助下载器`。
- 首次使用请按安装完成提示手动加载 `browser-extension` 文件夹到 Chrome/Edge 开发者模式。
