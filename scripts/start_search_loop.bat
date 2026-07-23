@echo off
cd /d "%~dp0.."
call .venv\Scripts\activate.bat
python -m app.main run-loop >> logs\search_loop.log 2>&1
