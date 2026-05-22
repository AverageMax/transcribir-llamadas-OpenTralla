@echo off
chcp 65001 > nul
title Instalando dependencias - OpenTralla
color 0A

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║   OpenTralla - Instalacion                        ║
echo  ║   Transcriptor de Llamadas en Español             ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
echo  Instalando dependencias necesarias...
echo  (La primera vez puede tardar varios minutos)
echo.

python -m pip install --upgrade pip --quiet

echo  [1/7] Instalando numpy...
pip install numpy --quiet
if %errorlevel% neq 0 ( echo  ERROR instalando numpy & pause & exit /b 1 )

echo  [2/7] Instalando PyAudioWPatch (captura WASAPI loopback)...
pip install pyaudiowpatch --quiet
if %errorlevel% neq 0 ( echo  ERROR instalando pyaudiowpatch & pause & exit /b 1 )

echo  [3/7] Instalando faster-whisper (motor de transcripcion)...
pip install faster-whisper --quiet
if %errorlevel% neq 0 ( echo  ERROR instalando faster-whisper & pause & exit /b 1 )

echo  [4/7] Instalando httpx compatible con Python 3.13+ (sin modulo cgi)...
pip install "httpx>=0.27.0" --quiet
if %errorlevel% neq 0 ( echo  ERROR instalando httpx & pause & exit /b 1 )

echo  [5/7] Instalando mss (captura de pantalla)...
pip install mss --quiet

echo  [6/7] Instalando opencv-python (codificacion de video)...
pip install opencv-python --quiet

echo  [7/7] Verificando ffmpeg...
if exist "%~dp0ffmpeg.exe" (
    echo  OK ffmpeg.exe ya existe en la carpeta del proyecto.
    goto :instalar_ok
)

where ffmpeg >nul 2>nul
if %errorlevel% equ 0 (
    echo  OK ffmpeg encontrado en el PATH del sistema.
    goto :instalar_ok
)

echo.
echo  ffmpeg no encontrado. Descargando automaticamente...
echo  (Son ~70 MB, puede tardar 1-2 minutos segun tu internet)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$url = 'https://github.com/BtbN/ffmpeg-builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip';" ^
  "$zip = '%~dp0\_ffmpeg_tmp.zip';" ^
  "$out = '%~dp0\_ffmpeg_tmp';" ^
  "Write-Host '  Descargando...';" ^
  "Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing;" ^
  "Write-Host '  Extrayendo...';" ^
  "Expand-Archive -Path $zip -DestinationPath $out -Force;" ^
  "$exe = Get-ChildItem -Path $out -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1;" ^
  "if ($exe) { Copy-Item $exe.FullName '%~dp0\ffmpeg.exe' -Force; Write-Host '  OK ffmpeg.exe copiado.'; } else { Write-Host '  ERROR: no se encontro ffmpeg.exe en el zip.'; }" ^
  "Remove-Item $zip -ErrorAction SilentlyContinue;" ^
  "Remove-Item $out -Recurse -Force -ErrorAction SilentlyContinue;"

if exist "%~dp0ffmpeg.exe" (
    echo  OK ffmpeg.exe descargado y listo.
) else (
    echo.
    echo  AVISO: No se pudo descargar ffmpeg automaticamente.
    echo  Descargalo manualmente desde https://ffmpeg.org
    echo  y coloca ffmpeg.exe en esta carpeta.
    echo  (La app funcionara sin video, solo guarda .txt y .wav)
)

:instalar_ok
echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║   OK Instalacion completada                          ║
echo  ║                                                      ║
echo  ║   Ahora ejecuta:  iniciar.bat                        ║
echo  ╚══════════════════════════════════════════════════════╝
echo.
echo  NOTA: El modelo Whisper (~500 MB para 'small') se descargara
echo  automaticamente la primera vez que abras la app.
echo  Solo necesitas internet esa primera vez.
echo.
pause
