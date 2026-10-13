@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === VideoCompact GUI (source) ===
echo.

rem Prefer "python"; fall back to the Windows "py" launcher.
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found in PATH. Install Python 3.11+ first.
        goto :end
    )
    set "PY=py -3"
)

if not exist "ffmpeg.exe" (
    echo [ERROR] ffmpeg.exe not found. Extract ffmpeg.7z to the project root first.
    goto :end
)
if not exist "ffprobe.exe" (
    echo [ERROR] ffprobe.exe not found. Extract ffprobe.7z to the project root first.
    goto :end
)

echo ==^> Starting GUI ... ^(close the window to exit^)
%PY% "src\gui.py"
if errorlevel 1 (
    echo.
    echo [ERROR] GUI exited with an error. See output above.
    goto :end
)

endlocal
exit /b 0

:end
echo.
pause
endlocal
