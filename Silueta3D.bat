@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Primera vez: se instala lo necesario.
    call instalar.bat || exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "programa\llavero_app.py"
