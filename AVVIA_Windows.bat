@echo off
cd /d "%~dp0"
call venv\Scripts\activate
python traduci_sottotitoli.py %*
if errorlevel 1 pause
