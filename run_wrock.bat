@echo off
title W.R.O.C.K. - Worlds Reset On Command, King
echo ===================================================
echo   W.R.O.C.K. (Worlds Reset On Command, King)
echo ===================================================
echo.

if not exist ".venv" (
    echo Creating Python virtual environment...
    python -m venv .venv
)

echo Installing dependencies...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet

echo.
echo Starting W.R.O.C.K. Clap Detection...
.\.venv\Scripts\python.exe wrock.py
pause
