@echo off
cd /d "%~dp0"
echo === AutoSub Studio CORE ===
where python >nul 2>nul
if errorlevel 1 (
  winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg bulunamadi. Kuruluyor...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  echo FFmpeg yeni kurulduysa bu pencereyi kapatip INSTALL_CORE.bat dosyasini bir kez daha calistir.
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip wheel
".venv\Scripts\python.exe" -m pip install -r requirements-core.txt
echo.
echo Core tamamlandi. Simdi INSTALL_TRANSLATION.bat calistir.
pause
