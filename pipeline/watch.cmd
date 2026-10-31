@echo off
rem Task Scheduler entry point: look once for new or changed lessons in Drive, then exit.
rem Register (every 10 minutes, only while logged in):
rem   schtasks /Create /TN "Easy mode watcher" /SC MINUTE /MO 10 /TR "D:\code\YT\pipeline\watch.cmd"
cd /d D:\code\YT\pipeline
D:\YT\work\venv\Scripts\python.exe -I easy.py watch "J:\My Drive\SINEM-YouTube" --once >> D:\YT\easy-work\watch.log 2>&1
