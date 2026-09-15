@echo off
REM Cria o ambiente virtual e instala as dependencias.
setlocal
cd /d "%~dp0"

set "PYLAUNCH="
where py >nul 2>nul && (
    py -3.12 -c "import tkinter" >nul 2>nul && set "PYLAUNCH=py -3.12"
    if not defined PYLAUNCH py -3 -c "import tkinter" >nul 2>nul && set "PYLAUNCH=py -3"
)
if not defined PYLAUNCH (
    where python >nul 2>nul && set "PYLAUNCH=python"
)
if not defined PYLAUNCH (
    echo Python 3.10+ nao encontrado. Instale em https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Usando: %PYLAUNCH%
if not exist ".venv\Scripts\python.exe" (
    %PYLAUNCH% -m venv .venv || (echo Falha ao criar o venv & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Falha ao instalar dependencias & pause & exit /b 1)

REM Commit com titulo de versao (ex.: 1.2.0) atualiza version.py; ver .githooks\post-commit.
if exist ".git" where git >nul 2>nul && git config core.hooksPath .githooks

echo.
if not exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    echo AVISO: Tesseract OCR nao encontrado. O mapeador de torneios precisa dele para ler os titulos.
    echo        Instale em https://github.com/UB-Mannheim/tesseract/wiki
)
echo Pronto. Uso diario: EventUploader.bat   Mapear torneios: MapearTorneios.bat
pause
endlocal
