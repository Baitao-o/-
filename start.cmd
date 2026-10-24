@echo off
chcp 65001 >nul
title 麦门省钱搭子 - 本地界面
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [错误] 没有找到 python 命令
    echo.
    echo   请先安装 Python 3.8 或更高版本，安装时勾选 "Add Python to PATH"
    echo   下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo.
echo   ==========================================================
echo     麦门省钱搭子 · 本地界面
echo   ==========================================================
echo   浏览器会自动打开。若没有，请手动访问下面打印的地址。
echo   （默认 http://127.0.0.1:8808/ ，端口被占用会自动换）
echo.
echo   关闭这个窗口 = 停止服务。
echo   ==========================================================
echo.

python web\server.py

echo.
echo   服务已停止。按任意键关闭窗口。
pause >nul
