@echo off
chcp 65001 >nul
echo ========================================
echo   Bambu Lab Telegram Bot
echo ========================================
echo.

REM Створюємо віртуальне оточення, якщо його немає
if not exist ".\.venv\Scripts\python.exe" (
    echo Віртуальне оточення не знайдено. Створюю...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ПОМИЛКА: не вдалося створити venv. Перевірте, що Python встановлено і доступний у PATH.
        pause
        exit /b 1
    )
    echo Встановлюю залежності...
    ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
    ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM Перевіряємо конфіг
if not exist "config.json" (
    echo.
    echo ПОМИЛКА: config.json не знайдено.
    echo Скопіюйте config.example.json у config.json і впишіть токен бота.
    pause
    exit /b 1
)

REM Запуск
".\.venv\Scripts\python.exe" telegram_bot.py

if errorlevel 1 (
    echo.
    echo Бот завершився з помилкою. Перевірте лог вище.
    pause
)

pause
