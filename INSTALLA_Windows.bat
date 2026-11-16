@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Installazione Traduttore Sottotitoli ===
where python >nul 2>nul || (echo Installa Python 3.10+ da https://www.python.org  ^(spunta "Add to PATH"^) & pause & exit /b)
python -m venv venv
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
rem ffmpeg è incluso (pacchetto imageio-ffmpeg)
where ollama >nul 2>nul || (echo. & echo Installo Ollama ^(AI locale^)... & winget install -e --id Ollama.Ollama)
echo.
echo Scarico il modello AI per la traduzione (circa 4.7 GB, altrimenti lo fa il programma al primo uso)...
ollama pull qwen2.5:7b
echo.
echo Installazione completata! Ora fai doppio clic su AVVIA_Windows.bat
pause
