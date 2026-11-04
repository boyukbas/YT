@echo off
rem The private dashboard https://yt.boyukbas.com: started hidden at logon (Task Scheduler "YT dashboard").
rem Runs the Cloudflare tunnel (config D:\YT\easy-work\cloudflared.yml) and the server (dashboard.py, 127.0.0.1:8790),
rem each restarted 30 s after it stops. Logs: D:\YT\easy-work\tunnel.log, dashboard-server.log.
cd /d "%~dp0"
if "%1"=="tunnel" goto tunnel
start "" /b cmd /c "%~f0" tunnel
:server
D:\YT\work\venv\Scripts\python.exe -I dashboard.py serve >> D:\YT\easy-work\dashboard-server.log 2>&1
ping -n 31 127.0.0.1 >nul
goto server
:tunnel
D:\YT\tools\cloudflared.exe tunnel --config D:\YT\easy-work\cloudflared.yml --no-autoupdate --loglevel warn run >> D:\YT\easy-work\tunnel.log 2>&1
ping -n 31 127.0.0.1 >nul
goto tunnel
