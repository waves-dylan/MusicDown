@echo off
chcp 65001 >nul
echo ========================================================
echo       MusicDownload 纯净虚拟环境与 UPX 压缩打包工具
echo ========================================================

set PATH=%~dp0tools\upx;%PATH%

if exist "%~dp0.venv\Scripts\python.exe" -m PyInstaller (
    echo [INFO] 检测到纯净虚拟环境 .venv，使用隔离环境打包...
    "%~dp0.venv\Scripts\python.exe" -m PyInstaller --clean musicdownload.spec
) else (
    echo [WARN] 未检测到 .venv，回退至系统全局 PyInstaller 打包...
    pyinstaller --clean musicdownload.spec
)

if %ERRORLEVEL% EQU 0 (
    echo [INFO] 正在同步成果至根目录...
    copy /y dist\musicdownload.exe .\musicdownload.exe
    echo [SUCCESS] 打包完成！输出文件: .\musicdownload.exe
) else (
    echo [ERROR] 打包失败，请检查上方日志。
)
pause
