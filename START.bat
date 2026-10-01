@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Once INSTALL_CORE.bat calistirilmali.
  pause
  exit /b 1
)
set "NVROOT=%~dp0.venv\Lib\site-packages\nvidia"
if exist "%NVROOT%\cublas\bin" set "PATH=%NVROOT%\cublas\bin;%PATH%"
if exist "%NVROOT%\cudnn\bin" set "PATH=%NVROOT%\cudnn\bin;%PATH%"
if exist "%NVROOT%\cuda_runtime\bin" set "PATH=%NVROOT%\cuda_runtime\bin;%PATH%"
".venv\Scripts\python.exe" app.py
