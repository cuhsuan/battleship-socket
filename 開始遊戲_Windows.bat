@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py client_gui.py) else (python client_gui.py)
if errorlevel 1 (
  echo.
  echo 啟動失敗：請先安裝 Python 3（python.org，安裝時勾選 Add Python to PATH）
  pause
)
