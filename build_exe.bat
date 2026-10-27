@echo off
chcp 65001 >nul
REM build_exe.bat -- double-click to repackage dist\NeonSweep.exe (onefile).
REM
REM Keep every REM (comment-only) line ASCII: cmd.exe misparses non-ASCII
REM bytes on bare REM lines even with chcp 65001 active, splitting the line
REM and throwing "is not recognized as an internal or external command".
REM "echo" lines with Chinese text are fine -- only bare REM lines broke.
REM Same reason there is no nested if/else with a Chinese echo inside: use
REM goto labels instead (same pattern as YungAnProject\M6000\build_exe.bat).
REM
REM What this does, in order:
REM   1. setup_env.bat: make sure .venv (pinned Python + requirements.txt) exists
REM   2. delete the old dist\NeonSweep.exe
REM   3. pyinstaller NeonSweep.spec --noconfirm --clean
REM   4. verify the new exe actually exists
REM
REM Deliberately does NOT open the dist folder at the end -- Explorer steals
REM focus / switches the active window, which is annoying mid-work.

setlocal
cd /d "%~dp0"

call "%~dp0setup_env.bat"
if errorlevel 1 goto :end

echo ============================================
echo  清除舊的 dist\NeonSweep.exe...
echo ============================================
if exist "dist\NeonSweep.exe" (
    del /f /q "dist\NeonSweep.exe"
    echo 已刪除 dist\NeonSweep.exe
) else (
    echo 沒有找到舊檔，略過
)
if exist "dist\NeonSweep.exe" goto :delete_failed

echo.
echo ============================================
echo  開始打包（pyinstaller NeonSweep.spec --noconfirm --clean）...
echo ============================================
pyinstaller NeonSweep.spec --noconfirm --clean

REM Check the real exit code first. A PyInstaller crash mid-build (transient
REM AV lock on the old exe, etc.) can leave a STALE exe from a previous run --
REM "if exist" alone would then falsely report success.
if errorlevel 1 goto :build_failed
if not exist "dist\NeonSweep.exe" goto :build_failed

echo.
echo ============================================
echo  打包成功：dist\NeonSweep.exe
echo ============================================
goto :end

:delete_failed
echo.
echo ============================================
echo  舊的 dist\NeonSweep.exe 刪不掉（可能正在執行或被防毒鎖住），已中止。
echo ============================================
goto :end

:build_failed
echo.
echo ============================================
echo  打包失敗，往上捲看錯誤訊息。
echo ============================================

:end
pause
