#!/usr/bin/env python3
"""
sync_nexus_kb.py
Copia a base de conhecimento do blis-nexus-hub (Supabase source)
→ Supabase do cx-portal (tabela kb_articles).

Os artigos ficam disponíveis em dash.blis.support na aba Base de Conhecimento.

Uso:
    python3 sync_github_zendesk.py

Pré-requisitos:
    pip install requests supabase python-dotenv
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Supabase SOURCE: blis-nexus-hub ───────────────────────────────────────────
NEXUS_URL  = "https://auzpgpwvmdhyrkyhzuuf.supabase.co/rest/v1"
NEXUS_ANON = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImF1enBncHd2bWRoeXJreWh6dXVmIiwicm9sZSI6ImFub24i"
    "LCJpYXQiOjE3NzMzNDg1MjEsImV4cCI6MjA4ODkyNDUyMX0"
    ".SnC4-yuuVggD8kNxjzabcroZm4wEYOLsm9k8PBmIfcU"
)

# ── Supabase DEST: cx-portal ──────────────────────────────────────────────────
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

TEAM_LABELS = {
    "saude":     "Blis Saúde",
    "resolve":   "Blis Resolve",
    "medicos":   "Médicos",
    "logistica": "Logística",
}


def get_dest() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórios no .env")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_nexus_articles() -> list[dict]:
    """Busca todos os artigos do blis-nexus-hub via REST (RLS público)."""
    url = f"{NEXUS_URL}/kb_articles?select=id,title,content,team_id,category,updated_at&order=team_id,category,title"
    headers = {
        "apikey": NEXUS_ANON,
        "Authorization": f"Bearer {NEXUS_ANON}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upsert_article(sb: Client, record: dict) -> None:
    sb.table("kb_articles").upsert(record, on_conflict="github_path").execute()


def insert_sync_log(sb: Client, record: dict) -> None:
    sb.table("kb_sync_log").insert(record).execute()


def main() -> None:
    print("=" * 60)
    print("Sincronização blis-nexus-hub → cx-portal (Base de Conhecimento)")
    print("=" * 60)

    t0 = time.time()
    now_utc = datetime.now(timezone.utc).isoformat()

    sb = get_dest()
    log.info("Supabase cx-portal conectado")

    # 1. Buscar artigos no nexus-hub
    print("\n[1/2] Buscando artigos no nexus-hub...")
    try:
        articles = fetch_nexus_articles()
    except Exception as e:
        print(f"ERRO ao buscar nexus-hub: {e}")
        return
    print(f"      {len(articles)} artigo(s) encontrado(s)")

    by_team: dict[str, int] = {}
    for a in articles:
        by_team[a["team_id"]] = by_team.get(a["team_id"], 0) + 1
    for team, n in sorted(by_team.items()):
        label = TEAM_LABELS.get(team, team)
        print(f"        {label}: {n}")

    # 2. Upsert no cx-portal
    print("\n[2/2] Atualizando kb_articles no cx-portal...\n")
    criados = atualizados = erros = 0

    for art in articles:
        # github_path serve como chave única estável: team_id/uuid
        path = f"{art['team_id']}/{art['id']}"
        team_label = TEAM_LABELS.get(art["team_id"], art["team_id"].title())
        title = art.get("title") or "—"

        try:
            existing = sb.table("kb_articles").select("id").eq("github_path", path).execute()
            action = "atualizado" if existing.data else "criado"

            upsert_article(sb, {
                "github_path":    path,
                "title":          title,
                "html_body":      art.get("content") or "",
                "status":         action,
                "error_msg":      None,
                "last_synced_at": now_utc,
            })

            if action == "criado":
                criados += 1
                print(f"  [CRIADO]     [{team_label}] {art.get('category','—')} / {title}")
            else:
                atualizados += 1
                print(f"  [ATUALIZADO] [{team_label}] {art.get('category','—')} / {title}")

        except Exception as e:
            erros += 1
            print(f"  [ERRO]       {title}: {e}")

    duracao = round(time.time() - t0, 1)

    insert_sync_log(sb, {
        "synced_at":   now_utc,
        "total_files": len(articles),
        "criados":     criados,
        "atualizados": atualizados,
        "erros":       erros,
        "duracao_s":   duracao,
    })

    print("\n" + "=" * 60)
    print("RELATÓRIO")
    print(f"  Artigos encontrados : {len(articles)}")
    print(f"  Criados             : {criados}")
    print(f"  Atualizados         : {atualizados}")
    print(f"  Erros               : {erros}")
    print(f"  Duração             : {duracao}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
