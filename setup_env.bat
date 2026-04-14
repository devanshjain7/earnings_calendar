@echo off
:: Script to setup the virtual environment 'poly' for the earnings terminal on Windows
IF EXIST poly (
    echo Virtual environment 'poly' already exists.
) ELSE (
    echo Creating virtual environment 'poly'...
    python -m venv poly
    call poly\Scripts\activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    echo Virtual environment 'poly' created and dependencies installed.
)
pause
