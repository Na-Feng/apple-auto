@echo off
chcp 65001 >nul
title iCloud 别名邮箱多账号自动管理平台
echo ===================================================
echo   正在启动 iCloud 别名邮箱多账号自动管理平台...
echo ===================================================
cd /d "%~dp0"
python main.py
if %errorlevel% neq 0 (
    echo.
    echo [错误] 启动失败，请检查 Python 是否已正确安装并添加到 PATH 环境变量。
    pause
)
