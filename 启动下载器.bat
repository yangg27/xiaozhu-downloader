@echo off
chcp 65001 >nul
title 小助下载器

rem ============================================================
rem  小助下载器 · 启动脚本
rem  默认：后台静默启动（无窗口，只留右下角托盘图标）
rem  想看到主界面：双击「打开下载器窗口.bat」
rem  首次使用：双击「一键设置.bat」配置开机自启与浏览器唤起
rem ============================================================

set "HERE=%~dp0"
set "PYEXE="

rem ---- 优先 pythonw.exe（无控制台窗口，静默的关键）----
for %%P in (
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\pythonw.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python311\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python313\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python312\pythonw.exe"
) do (
  if not defined PYEXE if exist %%P set "PYEXE=%%~P"
)

rem ---- 回退：找带 tkinter 的 python.exe ----
if not defined PYEXE (
  for %%P in (
    "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
    "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
    "C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"
    "%LocalAppData%\Programs\Python\Python313\python.exe"
  ) do (
    if not defined PYEXE if exist %%P set "PYEXE=%%~P"
  )
)

if not defined PYEXE (
  echo.
  echo   [错误] 没有找到 Python 3 环境。
  echo   请安装 Python 3（需带 tcl/tk 组件）。
  echo.
  pause
  exit /b 1
)

rem ---- 检查 tkinter 可用 ----
"%PYEXE%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [错误] 当前 Python 缺少 tkinter： "%PYEXE%"
  echo   请重装 Python 3 并勾选 tcl/tk 组件。
  echo.
  pause
  exit /b 1
)

rem ---- 已在运行则直接提示，避免重复启动 ----
"%PYEXE%" -c "import socket,sys; s=socket.socket(); s.settimeout(0.6); sys.exit(0 if s.connect_ex(('127.0.0.1',8763))==0 else 1)" >nul 2>&1
if not errorlevel 1 (
  echo.
  echo   小助下载器已在后台运行中。
  echo   可在任务栏右下角托盘找到图标；双击图标可打开主窗口。
  echo.
  timeout /t 3 >nul
  exit /b 0
)

rem ---- 静默后台启动 ----
start "" "%PYEXE%" "%HERE%gui.py" --silent

echo.
echo   小助下载器已在后台启动。
echo   · 托盘图标位于任务栏右下角（可能需点开"隐藏的图标"）
echo   · 浏览器点击下载链接会自动转交本程序
echo   · 双击「打开下载器窗口.bat」可查看任务列表
echo.
timeout /t 4 >nul
exit /b 0