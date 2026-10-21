@echo off
chcp 65001 > nul
title 喵想奇记打包程序

echo 正在安装依赖库...
pip install pyinstaller pynput pillow

echo 正在检查图标文件...
if not exist "app_icon.ico" (
    echo 错误：未找到图标文件 app_icon.ico！
    pause
    exit /b 1
)

echo 正在打包应用程序...
pyinstaller --onefile ^
            --windowed ^
            --icon=app_icon.ico ^
            --name "喵想奇记" ^
            --clean ^
            --noconfirm ^
            automation_app.py

if errorlevel 1 (
    echo 打包过程中出现错误！
    pause
    exit /b 1
)

echo.
echo 打包成功完成！
echo 可执行文件路径：%~dp0dist\喵想奇记.exe
echo.
echo 注意事项：
echo 1. 请确保 scripts 目录与可执行文件在相同目录
echo 2. 首次运行时可能需要以管理员权限运行
echo 3. 杀毒软件可能会误报，请添加信任
echo.
pause