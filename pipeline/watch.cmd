@echo off
rem Task Scheduler runs this every 10 minutes (hidden: conhost --headless). Output goes to D:\YT\easy-work\watch.log.
cd /d "%~dp0"
echo ==== %date% %time% >> D:\YT\easy-work\watch.log
D:\YT\work\venv\Scripts\python.exe -I easy.py watch "J:\My Drive\SINEM-YouTube" --once >> D:\YT\easy-work\watch.log 2>&1
