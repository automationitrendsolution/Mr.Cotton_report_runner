@echo off
rem Windows Task Scheduler entry point.
cd /d "%~dp0"
uv run run_reports.py %*
