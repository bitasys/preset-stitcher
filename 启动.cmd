@echo off
cd /d "%~dp0"
py -3 preset_manager.py
if errorlevel 1 pause
