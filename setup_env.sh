#!/bin/bash

# Script to setup the virtual environment 'poly' for the earnings terminal on Unix-like systems
VENV_NAME="venv"

if [ -d "$VENV_NAME" ]; then
    echo "Virtual environment '$VENV_NAME' already exists."
else
    echo "Creating virtual environment '$VENV_NAME'..."
    python3 -m venv "$VENV_NAME"
    
    # Check if venv creation was successful
    if [ $? -eq 0 ]; then
        source "$VENV_NAME/bin/activate"
        python3 -m pip install --upgrade pip
        pip install -r requirements.txt
        echo "Virtual environment '$VENV_NAME' created and dependencies installed."
    else
        echo "Error: Failed to create virtual environment."
        exit 1
    fi
fi
