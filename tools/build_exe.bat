@echo off
REM ====================================================================
REM  Build CBT.exe  (developer machine only - not shipped)
REM
REM  Uses a SEPARATE build venv rather than the runtime venv. Installing
REM  PyInstaller into the runtime environment would change the
REM  requirements fingerprint on every user machine and pull a build-time
REM  tool into a shipped, air-gapped product.
REM
REM  Requires internet access ONCE to fetch PyInstaller, or a vendored
REM  wheel - see the --no-index variant at the bottom of this file.
REM ====================================================================

setlocal EnableExtensions

set "TOOLS_DIR=%~dp0"
set "PACKAGE_ROOT=%TOOLS_DIR%.."
set "BUILD_VENV=%TEMP%\CBT_build_venv"
set "BOOTSTRAP_PY=%PACKAGE_ROOT%\CBT\native_host\Python310\python.exe"

if not exist "%BOOTSTRAP_PY%" (
    echo [ERROR] Bundled interpreter not found: %BOOTSTRAP_PY%
    exit /b 2
)

echo === Creating build environment ===
if not exist "%BUILD_VENV%\Scripts\python.exe" (
    "%BOOTSTRAP_PY%" -m venv "%BUILD_VENV%" || exit /b 1
)

echo === Installing PyInstaller ===
"%BUILD_VENV%\Scripts\python.exe" -m pip install --upgrade pip pyinstaller || exit /b 1
REM  Air-gapped alternative (vendor the wheels into tools\build_packages):
REM  "%BUILD_VENV%\Scripts\python.exe" -m pip install --no-index ^
REM      --find-links="%TOOLS_DIR%build_packages" pyinstaller || exit /b 1

echo === Building CBT.exe ===
pushd "%PACKAGE_ROOT%"
"%BUILD_VENV%\Scripts\pyinstaller.exe" "tools\CBT.spec" --noconfirm --clean --distpath "build_out\dist" --workpath "build_out\work"
set "RC=%ERRORLEVEL%"
popd

if not "%RC%"=="0" (
    echo [ERROR] Build failed with code %RC%.
    exit /b %RC%
)

echo === Installing executable into the package root ===
copy /Y "%PACKAGE_ROOT%\build_out\dist\CBT.exe" "%PACKAGE_ROOT%\CBT.exe" >nul || exit /b 1

echo.
echo Build complete: %PACKAGE_ROOT%\CBT.exe
echo Create the desktop shortcut with:
echo     "%PACKAGE_ROOT%\create-shortcut.bat" exe
echo   (or: powershell -ExecutionPolicy Bypass -File "%TOOLS_DIR%create_shortcut.ps1" -Target exe)
echo.

endlocal & exit /b 0
