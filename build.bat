@echo off
REM ============================================================
REM  CrabClaw 构建脚本（Windows）
REM  在 ollama-agent 项目根目录运行：build.bat
REM  产出：dist\CrabClaw.exe（便携版） 与  dist\CrabClaw_Setup.exe（安装包）
REM ============================================================
setlocal
cd /d %~dp0

REM 1) 选择 Python 解释器（优先用项目自带的 .venv，否则用系统 python）
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM 2) 安装依赖（防止漏装 requests 等模块导致打包后启动崩溃）
echo 正在检查/安装依赖（requirements.txt）...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络或 Python 环境。
    exit /b 1
)

REM 3) 构建 exe
REM    说明：不使用 --clean（部分环境的安全删除策略会拦截），改用固定 workpath/distpath
if not exist build mkdir build
%PY% -m PyInstaller CrabClaw.spec --workpath build --distpath dist --noconfirm
if errorlevel 1 (
    echo [错误] PyInstaller 构建失败，请检查上面的输出。
    exit /b 1
)

REM 4) 构建安装包（自动查找 NSIS 的 makensis.exe）
set "MAKENSIS="
if exist "%ProgramFiles%\NSIS\makensis.exe"      set "MAKENSIS=%ProgramFiles%\NSIS\makensis.exe"
if exist "%ProgramFiles(x86)%\NSIS\makensis.exe" set "MAKENSIS=%ProgramFiles(x86)%\NSIS\makensis.exe"
if exist "C:\Program Files\NSIS\makensis.exe"    set "MAKENSIS=C:\Program Files\NSIS\makensis.exe"

if "%MAKENSIS%"=="" (
    echo [跳过] 未找到 NSIS（makensis.exe），未生成安装包。
    echo         如需安装包，请安装 NSIS 后重新运行本脚本。
) else (
    "%MAKENSIS%" CrabClaw.nsi
    if errorlevel 1 (
        echo [错误] NSIS 打包失败，请检查上面的输出。
        exit /b 1
    )
)

echo.
echo 构建完成：
echo   dist\CrabClaw.exe
if not "%MAKENSIS%"=="" echo   dist\CrabClaw_Setup.exe
goto :eof
