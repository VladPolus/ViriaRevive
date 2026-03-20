@echo off
echo ============================================
echo   ViriaRevive — Windows Startup Setup
echo ============================================
echo.
echo Choose an option:
echo   [1] Enable auto-start on Windows login (minimized to tray)
echo   [2] Disable auto-start
echo   [3] Cancel
echo.
set /p choice="Enter choice (1/2/3): "

if "%choice%"=="1" goto enable
if "%choice%"=="2" goto disable
if "%choice%"=="3" goto done
echo Invalid choice.
goto done

:enable
echo.
echo Creating startup shortcut...

set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP_FOLDER%\ViriaRevive.lnk"
set "VBS_PATH=%~dp0ViriaRevive_Startup.vbs"

REM Create a shortcut using PowerShell (works on all Windows 10/11)
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = 'wscript.exe'; $s.Arguments = '\"%VBS_PATH%\"'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'ViriaRevive - Auto-start minimized'; $s.Save()"

if exist "%SHORTCUT%" (
    echo.
    echo [OK] ViriaRevive will now auto-start when you log in.
    echo      It launches minimized to the system tray.
    echo      Shortcut: %SHORTCUT%
) else (
    echo [ERROR] Failed to create startup shortcut.
)
goto done

:disable
echo.
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ViriaRevive.lnk"
if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo [OK] Auto-start disabled. Shortcut removed.
) else (
    echo [OK] Auto-start was not enabled. Nothing to remove.
)
goto done

:done
echo.
pause
