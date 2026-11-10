@echo off
@rem run from the script's own folder: cookies and CSVs are saved relative to the working directory
cd /d "%~dp0"
echo Installing dependencies...
py -3 -m pip install -r requirements.txt
echo Running
py -3 humblesteamkeysredeemer.py
set /p=Press ENTER to close terminal
