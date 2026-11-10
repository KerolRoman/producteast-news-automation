@echo off
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist .env (
  copy .env.example .env
  echo.
  echo Add your OpenAI API key to .env, then run this file again.
  notepad .env
  pause
  exit /b 0
)
python generate_article.py
if errorlevel 1 (
  echo Article generation failed. Check API key, model, internet, or API billing.
  pause
  exit /b 1
)
echo.
echo Article is saved under output\articles
pause
