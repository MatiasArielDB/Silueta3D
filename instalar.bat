@echo off
rem Crea el entorno de Python e instala lo necesario (solo hace falta la primera vez).
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python. Instalalo desde https://www.python.org/downloads/ ^(marcar "Add python.exe to PATH"^).
    if not defined CI pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Creando el entorno...
    python -m venv .venv || goto :error
)
echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
echo.
echo Listo. Ahora abri el programa con Silueta3D.bat
if not defined CI pause
exit /b 0

:error
echo.
echo Hubo un error durante la instalacion.
if not defined CI pause
exit /b 1
