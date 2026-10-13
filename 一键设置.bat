@echo off
chcp 65001 >nul
title 小助下载器 · 一键设置
cd /d "%~dp0"

rem ============================================================
rem  一键设置：
rem    1. 开启开机自启（后台静默，不弹窗）
rem    2. 注册 xzd:// 协议（浏览器可自动唤起下载器）
rem    3. 立即在后台启动下载器
rem  全部写入当前用户范围，不需要管理员权限。
rem ============================================================

set "PYEXE="
for %%P in (
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
  "%LocalAppData%\Programs\Python\Python313\python.exe"
  "%LocalAppData%\Programs\Python\Python312\python.exe"
) do (
  if not defined PYEXE if exist %%P set "PYEXE=%%~P"
)
if not defined PYEXE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
  )
)
if not defined PYEXE (
  echo.
  echo   [错误] 没有找到 Python 3 环境。
  echo.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   小助下载器 · 一键设置
echo ============================================================
echo.
echo  [1/3] 开启开机自启与协议注册...
"%PYEXE%" "%~dp0sysintegration.py" install
echo.
echo  [2/3] 后台启动下载器...
"%PYEXE%" "%~dp0sysintegration.py" start
echo.
echo  [3/3] 当前状态：
"%PYEXE%" "%~dp0sysintegration.py"
echo.
echo ============================================================
echo   设置完成！接下来只需装一次浏览器扩展：
echo.
echo   Chrome:  地址栏输入  chrome://extensions
echo   Edge:    地址栏输入  edge://extensions
echo.
echo   打开「开发者模式」→「加载已解压的扩展程序」
echo   选择本目录下的  browser-extension  文件夹
echo.
echo   装好后：浏览网页点下载链接 → 自动转交本程序满速下载
echo ============================================================
echo.
pause