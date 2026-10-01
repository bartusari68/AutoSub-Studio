@echo off
title AutoSub Studio Setup
cd /d "%~dp0"

echo ==========================================
echo          AutoSub Studio Setup
echo ==========================================
echo.

echo [1/2] Core components are being installed...
call "%~dp0INSTALL_CORE.bat"

echo.
echo [2/2] Translation components are being installed...
call "%~dp0INSTALL_TRANSLATION.bat"

echo.
echo ==========================================
echo Setup completed.
echo ==========================================
echo.
echo NVIDIA GPU users can optionally run CUDA_SETUP.bat.
echo.
echo Start AutoSub Studio with START.bat.
echo.

pause
