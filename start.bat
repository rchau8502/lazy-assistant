@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 Python 3，请先安装：https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv python -m venv .venv
.venv\Scripts\python -c "import fastapi, uvicorn, httpx, dotenv" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，安装依赖（几十秒）...
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt
)
if not exist .env .venv\Scripts\python setup.py
set PORT=8010
for /f "tokens=2 delims==" %%a in ('findstr /b "PORT=" .env') do set PORT=%%a
start "" http://127.0.0.1:%PORT%
echo 启动：http://127.0.0.1:%PORT%   按 Ctrl+C 停止
.venv\Scripts\uvicorn app:app --host 0.0.0.0 --port %PORT%
pause
