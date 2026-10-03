@echo off
rem comic_dedupe GUI launcher (ASCII only: cmd reads .bat as cp932)
cd /d "%~dp0.."
py -m comic_dedupe.gui
if errorlevel 1 (
    echo.
    echo Launch failed. If this is the first run, execute:
    echo     py -m pip install -r "%~dp0requirements.txt"
    pause
)
