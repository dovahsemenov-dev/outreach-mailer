@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo === OUTREACH MAILER: отправка откликов по email ===
python main.py
echo.
echo === Готово. Журнал: sent_log.csv - логи: logs\app.log ===
pause
