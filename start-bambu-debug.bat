@echo off
REM Запускає Bambu Farm Manager Client у debug-режимі для farmwatch.
REM Закриває всі поточні інстанси (single-instance lock), потім стартує з CDP-портом 9222.
REM Запускай ВІД АДМІНА (ПКМ -> Run as administrator), щоб гарантовано закрити старий клієнт.

set EXE="C:\Program Files\Bambu Farm Manager Client\Bambu Farm Manager Client.exe"

echo Closing existing client...
taskkill /F /IM "Bambu Farm Manager Client.exe" /T >nul 2>&1
timeout /t 3 /nobreak >nul

echo Starting client with debug port 9222...
start "" %EXE% --remote-debugging-port=9222 --remote-allow-origins=*

echo.
echo Done. The client should reopen and reconnect to the farm.
echo Leave it running; farmwatch (and Claude) can now read it on port 9222.
timeout /t 4 /nobreak >nul
