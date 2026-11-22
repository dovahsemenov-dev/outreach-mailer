@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo === PANEL OUTREACH MAILER ===
echo Browser will open by itself. Keep this window open while working.
echo Close panel: close this window or Ctrl+C here.
python panel.py
pause
