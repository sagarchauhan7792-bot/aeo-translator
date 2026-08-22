@echo off
REM Blog Studio launcher. Starts the local server and opens the browser.
cd /d "%~dp0"
python -m studio %*
pause
