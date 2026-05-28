#!/bin/bash
# Atualização diária — roda às 7h via crontab
# Pipeline: Zendesk/Metabase → output/ → FTP → Supabase

set -e

PROJECT="/Users/sol/Downloads/projetos/dash-blis-cx"
LOG="$PROJECT/output/supabase_load.log"
PYTHON="python3"

cd "$PROJECT"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') INÍCIO ===" >> "$LOG"

# 1. Busca dados e sobe pro FTP
echo "[1/3] updater.py..." >> "$LOG"
$PYTHON "$PROJECT/updater.py" >> "$LOG" 2>&1

# 2. Carrega Zendesk pro Supabase
echo "[2/3] db_loader_zendesk.py..." >> "$LOG"
$PYTHON "$PROJECT/db_loader_zendesk.py" >> "$LOG" 2>&1

# 3. Carrega Metabase pro Supabase
echo "[3/3] db_loader_metabase.py..." >> "$LOG"
$PYTHON "$PROJECT/db_loader_metabase.py" >> "$LOG" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') CONCLUÍDO ===" >> "$LOG"
