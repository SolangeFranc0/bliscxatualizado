#!/bin/bash
# Atualização diária — roda às 7h via crontab
# Pipeline: Zendesk/Metabase → output/ → FTP → Supabase

set -e

PROJECT="/Users/sol/Downloads/projetos/dash-blis-cx"
LOG="$PROJECT/output/supabase_load.log"
PYTHON="python3"

cd "$PROJECT"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') INÍCIO ===" >> "$LOG"

# 1. Pipeline completo: Zendesk + Metabase + Supabase + FTP
echo "[1/1] updater.py..." >> "$LOG"
$PYTHON "$PROJECT/updater.py" >> "$LOG" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') CONCLUÍDO ===" >> "$LOG"
