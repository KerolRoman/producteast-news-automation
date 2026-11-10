@echo off
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
if errorlevel 1 (
  echo Collector failed. See collector.log
  pause
  exit /b 1
)
python cluster_stories.py
if errorlevel 1 (
  echo Clustering failed.
  pause
  exit /b 1
)
echo.
echo Stage 2 completed. Check output for clusters_*.md
pause
