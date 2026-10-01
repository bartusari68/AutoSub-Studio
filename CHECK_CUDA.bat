@echo off
cd /d "%~dp0"
set "NVROOT=%~dp0.venv\Lib\site-packages\nvidia"
if exist "%NVROOT%\cublas\bin" set "PATH=%NVROOT%\cublas\bin;%PATH%"
if exist "%NVROOT%\cudnn\bin" set "PATH=%NVROOT%\cudnn\bin;%PATH%"
if exist "%NVROOT%\cuda_runtime\bin" set "PATH=%NVROOT%\cuda_runtime\bin;%PATH%"
nvidia-smi
echo.
".venv\Scripts\python.exe" -c "import ctranslate2; print('CUDA device count =', ctranslate2.get_cuda_device_count()); print('CUDA compute types =', ctranslate2.get_supported_compute_types('cuda'))"
pause
