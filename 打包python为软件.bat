@echo off
setlocal
cd /d "%~dp0"
title 喵想奇记打包程序

echo ============================================
echo     喵想奇记 一键打包
echo ============================================
echo.

rem ---------- 0. 检查源文件 ----------
if not exist "automation_app.py" (
    echo [错误] 当前目录没有 automation_app.py
    echo        请把本 bat 放到 automation_app.py 同一目录再运行。
    pause
    exit /b 1
)
if not exist "app_icon.ico" (
    echo [错误] 没有找到图标文件 app_icon.ico
    pause
    exit /b 1
)

rem ---------- 1. 找可用的 Python ----------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo [错误] 没找到 Python。请先安装 Python 3，安装时勾选 Add Python to PATH。
    pause
    exit /b 1
)
echo [1/5] 使用的 Python:
%PY% --version
if errorlevel 1 (
    echo [错误] Python 无法正常运行，请检查安装。
    pause
    exit /b 1
)
echo.

rem ---------- 2. 先关掉正在运行的旧程序（关键步骤） ----------
echo [2/5] 关闭可能正在运行的旧程序...
taskkill /F /IM "喵想奇记.exe" >nul 2>nul
if errorlevel 1 (
    echo        没有正在运行的旧程序。
) else (
    echo        检测到旧程序在运行，已自动关闭（否则 exe 被占用会导致打包失败）。
    ping -n 3 127.0.0.1 >nul
)
echo.

rem ---------- 3. 检查 / 安装打包依赖 ----------
echo [3/5] 检查打包依赖（首次需要联网，可能稍慢）...
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo        未安装 PyInstaller，正在安装...
    %PY% -m pip install --disable-pip-version-check pyinstaller
)
%PY% -c "import pynput, PIL, cv2, numpy" >nul 2>nul
if errorlevel 1 (
    echo        缺少运行依赖，正在补齐...
    %PY% -m pip install --disable-pip-version-check pynput pillow opencv-python numpy
)
echo        依赖检查完成。
echo.

rem ---------- 4. 打包 ----------
echo [4/5] 开始打包（约 1-3 分钟，请勿关闭窗口）...
if exist "build" rmdir /s /q "build" >nul 2>nul
%PY% -m PyInstaller --onefile --windowed --icon=app_icon.ico --name "喵想奇记" --add-data "scripts;scripts" --clean --noconfirm automation_app.py
if errorlevel 1 goto BUILD_FAIL
if not exist "dist\喵想奇记.exe" goto BUILD_FAIL

rem ---------- 5. 完成 ----------
echo.
echo [5/5] 打包成功!
echo        输出文件: %CD%\dist\喵想奇记.exe
for %%F in ("dist\喵想奇记.exe") do echo        文件大小: %%~zF 字节
echo.
echo 使用提示:
echo    1. 双击没反应时，先检查是否被杀毒软件拦截（添加信任即可）
echo    2. 需要操作管理员窗口的功能，请右键 exe 选择"以管理员身份运行"
echo    3. 截图识别的图片与任务配置保存在 %APPDATA%\MiaoXiangQiJi 目录
echo.
pause
exit /b 0

:BUILD_FAIL
echo.
echo ============ 打包失败 ============
echo 常见原因与处理办法:
echo    1. dist\喵想奇记.exe 正在运行被占用 - 先关掉程序再打包（本脚本已自动关闭，
echo       若仍失败请用任务管理器结束 喵想奇记.exe 再试）
echo    2. 没有 PyInstaller - 命令行执行: %PY% -m pip install pyinstaller
echo    3. 缺少依赖库 - 命令行执行: %PY% -m pip install pynput pillow opencv-python numpy
echo    4. 杀毒软件拦截打包过程 - 临时关闭杀毒或把本文件夹加入信任
echo    5. 路径含特殊字符 - 把整个文件夹移到纯英文路径（如 D:/miaoxiang）再试
echo.
echo 把本窗口里的报错内容截图发给我，我可以直接定位问题。
echo.
pause
exit /b 1
