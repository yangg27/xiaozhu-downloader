@echo off
chcp 65001 >nul
title 打开小助下载器窗口
cd /d "%~dp0"

rem 向运行中的实例请求显示主窗口（通过托盘），若无实例则正常启动并显示窗口

set "PYEXE="
for %%P in (
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\pythonw.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python311\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python313\pythonw.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
  "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
) do (
  if not defined PYEXE if exist %%P set "PYEXE=%%~P"
)
if not defined PYEXE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
  )
)
if not defined PYEXE (
  echo   未找到 Python 3，无法启动。
  pause
  exit /b 1
)

start "" "%PYEXE%" "%~dp0gui.py"
exit /b 0