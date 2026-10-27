@echo off
REM setup_env.bat -- called by build_exe.bat. Prepares an isolated .venv so the
REM build does not depend on whatever Python/packages this PC happens to have.
REM
REM   1. find Python %PYVER% (py launcher first, then plain python)
REM   2. create .venv with it (first run only)
REM   3. pip install -r requirements.txt (first run, and whenever it changes)
REM   4. put .venv\Scripts first on PATH so "python" / "pyinstaller" in the
REM      calling bat resolve to the .venv copies
REM
REM Deliberately NO setlocal: the PATH change must survive back into the caller.
REM Keep REM lines ASCII (cmd.exe misparses non-ASCII on bare REM lines).
REM To move to another Python: change PYVER, delete .venv, run build_exe.bat.

set "PYVER=3.14"
set "BASEPY="

py -%PYVER% --version >nul 2>nul
if not errorlevel 1 set "BASEPY=py -%PYVER%"
if defined BASEPY goto :have_py
python --version 2>nul | findstr /b /c:"Python %PYVER%." >nul
if not errorlevel 1 set "BASEPY=python"
if not defined BASEPY goto :no_python

:have_py
if not exist ".venv\Scripts\python.exe" goto :make_venv
".venv\Scripts\python.exe" --version 2>nul | findstr /b /c:"Python %PYVER%." >nul
if errorlevel 1 goto :venv_mismatch
goto :venv_ready

:make_venv
echo ============================================
echo  第一次執行：建立虛擬環境 .venv（Python %PYVER%）...
echo ============================================
%BASEPY% -m venv .venv
if errorlevel 1 goto :venv_failed

:venv_ready
REM .venv\requirements.installed = copy of requirements.txt from the last good install
fc /b "requirements.txt" ".venv\requirements.installed" >nul 2>nul
if not errorlevel 1 goto :deps_ready
echo ============================================
echo  安裝相依套件（requirements.txt，第一次需要連網，約 1-3 分鐘）...
echo ============================================
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
copy /y "requirements.txt" ".venv\requirements.installed" >nul

:deps_ready
set "VIRTUAL_ENV=%CD%\.venv"
set "PATH=%CD%\.venv\Scripts;%PATH%"
exit /b 0

:no_python
echo.
echo ============================================
echo  找不到 Python %PYVER%。請安裝（二擇一）：
echo    1. winget install Python.Python.%PYVER%
echo    2. python.org 下載 %PYVER%.x，安裝時勾選 "py launcher"
echo  裝好後重新執行這支 bat。
echo ============================================
exit /b 1

:venv_mismatch
echo.
echo ============================================
echo  現有的 .venv 不是 Python %PYVER% 建的。
echo  請刪除 .venv 資料夾後重新執行，會自動重建。
echo ============================================
exit /b 1

:venv_failed
echo.
echo ============================================
echo  建立 .venv 失敗，往上捲看錯誤訊息。
echo ============================================
exit /b 1

:pip_failed
echo.
echo ============================================
echo  pip 安裝相依套件失敗（網路？），往上捲看錯誤訊息。
echo  若訊息提到 No such file / long path：專案資料夾路徑太深（Windows 限制 260 字元），
echo  請把專案放到較短的路徑（例如 C:\Projects\名稱）再試。
echo ============================================
exit /b 1
