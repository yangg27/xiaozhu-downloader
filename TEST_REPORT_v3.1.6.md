# 小助下载器 v3.1.6 真机测试报告

- 测试时间：2026-10-26 02:34:54
- 通过：15
- 失败：0

## 详细日志

- [INFO] ============================================================
- [INFO] 小助下载器 v3.1.6 完整全功能真机测试开始
- [INFO] ============================================================
- [INFO] 开始安装测试
- [INFO] 安装目录: C:\Program Files\XZDM
- [PASS] 主程序存在: True
- [PASS] 卸载器存在: True
- [PASS] 开机自启注册表: True
- [PASS] xzd:// 协议注册: True
- [PASS] 卸载项注册: True
- [INFO] 启动与本地 API 测试
- [PASS] 本地 API 127.0.0.1:8763 可达: True
- [INFO] /ping 响应: {"ok": true, "app": "小助下载器", "version": "3.1.6", "port": 8763, "token": "5548b9c875fd664929524b3ac88236f2"}
- [PASS] 已获取 API token: True
- [INFO] HTTP 下载 API 测试
- [INFO] /add 响应: {"ok": true, "tid": "dbca98fb0cdc", "filename": "http-test.bin", "kind": "file", "url": "http://127.0.0.1:12568/http-test.bin"}
- [INFO] HTTP 任务完成: done, filename=http-test.bin
- [PASS] 下载文件 MD5 一致: True (C:\Users\Sunny_yang\AppData\Local\Temp\xzd_full_machine_test\downloads\http\http-test.bin)
- [INFO] aria2c 备用通道测试
- [INFO] aria2 /add 响应: {"ok": true, "tid": "ae5d4fe31f34", "filename": "aria2-test.bin", "kind": "file", "url": "http://127.0.0.1:12592/aria2-test.bin"}
- [INFO] aria2 任务完成: done, filename=aria2-test.bin
- [PASS] aria2 下载文件 MD5 一致: True (C:\Users\Sunny_yang\AppData\Local\Temp\xzd_full_machine_test\downloads\aria2\aria2-test.bin)
- [INFO] 帮助页面 /help 端点测试
- [PASS] GET /help 可达
- [INFO] BT/磁力输入解析测试（不依赖真实网络）
- [INFO] 磁力 /add 响应: {"ok": true, "tid": "3fd45de8a503", "filename": "download", "kind": "bt", "url": "magnet:?xt=urn:btih:0000000000000000000000000000000000000000&dn=test"}
- [PASS] 磁力链 API 可达
- [INFO] 卸载测试
- [PASS] 安装目录已删除: True
- [PASS] 开机自启已清理: True
- [PASS] xzd 协议已清理: True
- [PASS] 卸载项已清理: True
- [INFO] ============================================================
- [INFO] 测试结束：通过 15，失败 0
- [INFO] ============================================================