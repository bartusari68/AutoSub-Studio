@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Once INSTALL_CORE.bat calistirilmali.
  pause
  exit /b 1
)
echo === CUDA 12 runtime / cuBLAS / cuDNN ===
".venv\Scripts\python.exe" -m pip install --upgrade nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cudnn-cu12
pause
