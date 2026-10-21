@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "ClashCheckup.exe" (
  start "" "ClashCheckup.exe"
  exit /b
)
echo ClashCheckup.exe not found, falling back to python...
python -u agscanner.py --port 8765
pause
