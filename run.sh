#!/usr/bin/env bash
# Cron entry point. Example crontab (runs daily at 09:00 server time):
#   0 9 * * * /home/ubuntu/mrcotton_report_runner/run.sh >> /home/ubuntu/mrcotton_report_runner/logs/cron.log 2>&1
set -e

cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
uv run run_reports.py "$@"
