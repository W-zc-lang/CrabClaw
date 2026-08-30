@echo off
REM 一键打包成 exe（需先 pip install pywebview pyinstaller）
python -m pip install -q pywebview pyinstaller
python build_exe.py
echo.
echo 打包完成，产物在 dist\LocalAIChat.exe
pause
