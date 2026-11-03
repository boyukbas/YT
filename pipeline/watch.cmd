@echo off
rem Task Scheduler runs this every 10 minutes (hidden: conhost --headless). Output goes to D:\YT\easy-work\watch.log.
rem Google Drive's letter can change after a restart (J: until 2026-11-02, G: on 2026-11-03): use whichever has the folder.
cd /d "%~dp0"
echo ==== %date% %time% >> D:\YT\easy-work\watch.log
set "ROOT="
for %%L in (J G H I K L M N O P Q R S T U V W X Y Z E F) do if not defined ROOT if exist "%%L:\My Drive\SINEM-YouTube\" set "ROOT=%%L:\My Drive\SINEM-YouTube"
if not defined ROOT set "ROOT=J:\My Drive\SINEM-YouTube"
D:\YT\work\venv\Scripts\python.exe -I easy.py watch "%ROOT%" --once >> D:\YT\easy-work\watch.log 2>&1
