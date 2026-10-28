@echo off
title 喵想奇记 - Win7 兼容版打包
echo ============================================
echo   喵想奇记 Win7 兼容版打包（Python 3.8.10）
echo   产物：dist\喵想奇记-win7.exe（可在 Win7 运行）
echo ============================================
echo.

cd /d "%~dp0"

if not exist "automation_app.py" (
    echo [错误] 当前目录没有 automation_app.py，请把本脚本放在项目根目录运行。
    pause
    exit /b 1
)
if not exist "app_icon.ico" (
    echo [错误] 当前目录没有 app_icon.ico。
    pause
    exit /b 1
)

echo [1/4] 检查 Python 3.8（Win7 兼容打包必须用 3.8，新版 Python 不支持 Win7）...
set PY38=
py -3.8 --version >nul 2>&1
if not errorlevel 1 set PY38=py -3.8
if not defined PY38 (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY38 (
            for /f "tokens=2" %%v in ('"%%i" -V 2^>nul') do (
                if "%%v"=="3.8.10" set PY38="%%i"
            )
        )
    )
)
if not defined PY38 (
    echo.
    echo [错误] 没有找到 Python 3.8。
    echo.
    echo 请先安装 Python 3.8.10（官方最后一个支持 Win7 的版本）：
    echo   64 位系统：https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
    echo   32 位系统：https://www.python.org/ftp/python/3.8.10/python-3.8.10-win32.exe
    echo （32 位打出来的 exe 在 32/64 位 Win7 上都能跑，兼容性最好）
    echo.
    echo 安装时勾选 "Add Python 3.8 to PATH"，装完重新运行本脚本。
    echo 它可以和电脑里已有的新版 Python 共存，互不影响。
    pause
    exit /b 1
)
for /f "tokens=*" %%p in ('%PY38% -c "import sys;print(sys.executable)"') do echo   使用：%PY38% (%%p)

echo.
echo [2/4] 结束正在运行的软件（避免打包时文件被占用）...
taskkill /F /IM "喵想奇记.exe" >nul 2>&1
taskkill /F /IM "喵想奇记-win7.exe" >nul 2>&1

echo [3/4] 安装 Win7 兼容版依赖（固定旧版本，每次运行都会校对/自动降到指定版本）...
%PY38% -m pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless >nul 2>&1
%PY38% -m pip install -r requirements-win7.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

echo.
echo [4/4] 开始打包...
%PY38% -m PyInstaller --onefile --windowed --icon=app_icon.ico --name "喵想奇记-win7" --add-data "scripts;scripts" --clean --noconfirm automation_app.py
if errorlevel 1 (
    echo.
    echo ============================================
    echo   打包失败！常见原因：
    echo   1. dist\ 目录下的 exe 正在运行（关掉再试）
    echo   2. 依赖安装不完整（重新运行本脚本）
    echo   3. 磁盘空间不足（onefile 打包需要约 500MB 临时空间）
    echo ============================================
    pause
    exit /b 1
)

echo.
echo ============================================
echo   打包成功！
echo   产物：dist\喵想奇记-win7.exe
echo   可复制到 Windows 7 / 10 / 11 电脑上直接运行
echo   （Win10/11 上也能跑，只是体积和速度不如新版包）
echo ============================================
pause
