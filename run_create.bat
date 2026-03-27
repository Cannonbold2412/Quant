@echo off
python create_copilot_instructions.py
if %ERRORLEVEL% EQU 0 (
    echo File created successfully
    del create_copilot_instructions.py
) else (
    echo Failed to create file
)
