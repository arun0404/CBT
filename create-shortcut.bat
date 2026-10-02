@echo off
REM ====================================================================
REM  CBT - create a Desktop shortcut
REM
REM  Thin wrapper around tools\create_shortcut.ps1 so the shortcut can be
REM  made by double-clicking this file, instead of typing a PowerShell
REM  command with an execution-policy override.
REM
REM  Usage:
REM      create-shortcut.bat                silent launcher (launch.vbs)
REM      create-shortcut.bat exe            point the shortcut at CBT.exe
REM      create-shortcut.bat bat            point it at run.bat (console)
REM      create-shortcut.bat startmenu      also add a Start Menu entry
REM      create-shortcut.bat exe startmenu  both of the above
REM
REM  The .ps1 does the real work (icon, working directory, the .lnk COM
REM  object) and validates its own arguments.
REM ====================================================================

setlocal EnableExtensions

set "PROJROOT=%~dp0"
set "SHORTCUT_PS1=%PROJROOT%tools\create_shortcut.ps1"

if not exist "%SHORTCUT_PS1%" (
    echo [ERROR] Shortcut script not found:
    echo         %SHORTCUT_PS1%
    echo         Run this from the CBT folder that has a "tools" subfolder.
    echo.
    pause
    exit /b 2
)

REM ---- map the plain-word arguments to the .ps1 switches -----------
REM   arg: vbs / exe / bat   -> -Target   (default: vbs)
REM   arg: startmenu         -> -StartMenu

set "TARGET=vbs"
set "START_MENU="

for %%A in (%*) do (
    if /I "%%~A"=="vbs"       set "TARGET=vbs"
    if /I "%%~A"=="exe"       set "TARGET=exe"
    if /I "%%~A"=="bat"       set "TARGET=bat"
    if /I "%%~A"=="startmenu" set "START_MENU=-StartMenu"
)

REM ---- retire a pre-rename shortcut if one is still lying around ---
for %%D in ("%USERPROFILE%\Desktop" "%USERPROFILE%\OneDrive\Desktop") do (
    if exist "%%~D\OfflineTTS.lnk" (
        del /q "%%~D\OfflineTTS.lnk" >nul 2>&1
        echo Removed old shortcut: %%~D\OfflineTTS.lnk
    )
)

echo Creating CBT Desktop shortcut (target: %TARGET%) ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%SHORTCUT_PS1%" -Target %TARGET% %START_MENU%
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Done - a "CBT" shortcut is now on your Desktop.
) else (
    echo [ERROR] Shortcut creation failed with exit code %RC%.
    echo         See the message above for the reason.
)

echo.
pause

endlocal & exit /b %RC%
