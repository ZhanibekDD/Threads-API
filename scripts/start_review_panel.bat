@echo off
cd /d "%~dp0.."
call .venv\Scripts\activate.bat
python -m app.main run-web >> logs\review_panel.log 2>&1
