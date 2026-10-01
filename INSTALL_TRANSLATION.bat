@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Once INSTALL_CORE.bat calistirilmali.
  pause
  exit /b 1
)
echo === Yerel NLLB ceviri motoru ===
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements-translation.txt
echo.
echo Ceviri bilesenleri kuruldu. Ilk ceviride NLLB modeli indirilecek.
pause
