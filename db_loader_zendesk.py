#!/usr/bin/env python3
"""
db_loader_zendesk.py
Carrega dados do Zendesk CX (CSV) para o Supabase.

Uso:
    python3 db_loader_zendesk.py

Pré-requisitos:
    pip install supabase pandas python-dotenv

Variáveis de ambiente (.env):
    SUPABASE_URL=https://auzpgpwvmdhyrkyhzuuf.supabase.co
    SUPABASE_SERVICE_KEY=<service_role_key do painel Supabase>
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL     = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
OUTPUT_DIR       = Path(__file__).parent / "output"
BATCH_SIZE       = 500
TODAY            = str(date.today())


# ── Utilitários ────────────────────────────────────────────────────────────────

def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError(
            "SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórios no .env\n"
            "Obtenha o service_role key em: Supabase → Project Settings → API"
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """Substitui NaN/inf por None (compatível com JSON/Supabase)."""
    import numpy as np
    df = df.replace([np.inf, -np.inf], None)
    return df.where(pd.notna(df), None)


def to_bool(val) -> bool | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def safe_int(val) -> int | None:
    try:
        f = float(val)
        return None if math.isnan(f) else int(f)
    except (TypeError, ValueError):
        return None


def _sanitize(records: list[dict]) -> list[dict]:
    """Remove NaN/inf e converte floats inteiros para int (compatível com bigint)."""
    import math
    clean = []
    for row in records:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    new_row[k] = None
                elif v == int(v):
                    new_row[k] = int(v)
                else:
                    new_row[k] = v
            else:
                new_row[k] = v
        clean.append(new_row)
    return clean


def upsert_batch(sb: Client, table: str, records: list[dict],
                 conflict_col: str | None = None) -> int:
    """Envia registros em lotes de BATCH_SIZE. Retorna total enviado."""
    if not records:
        return 0
    records = _sanitize(records)
    sent = 0
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i : i + BATCH_SIZE]
        if conflict_col:
            sb.table(table).upsert(chunk, on_conflict=conflict_col).execute()
        else:
            sb.table(table).upsert(chunk).execute()
        sent += len(chunk)
    return sent


# ── Loaders individuais ────────────────────────────────────────────────────────

def load_tickets(sb: Client) -> None:
    path = OUTPUT_DIR / "tabela_tickets.csv"
    if not path.exists():
        log.warning("tabela_tickets.csv não encontrada — pulando")
        return

    df = pd.read_csv(path, low_memory=False)
    df = clean_df(df)

    bool_cols = ("atendido_por_ia", "transferido_n2", "resolvido_fcr")
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].apply(to_bool)

    ticket_cols = [
        "ticket_id", "criado_em", "criado_em_brt", "atualizado_em",
        "status", "prioridade", "canal", "assunto", "tags",
        "group_id", "nome_grupo", "time", "assignee_id",
        "requester_id", "organization_id",
        "atendido_por_ia", "transferido_n2", "resolvido_fcr",
        "ano_mes", "nome_mes", "semana_iso", "dia_semana",
        "motivo", "motivo_tag", "submotivo_tag", "submotivo_field_id",
        "perfil", "nome_agente",
        "primeira_resposta_min", "primeira_resposta_biz_min",
        "resolucao_min", "resolucao_biz_min",
        "pendencia_min", "pendencia_biz_min",
        "primeira_resposta_h", "resolucao_h", "pendencia_h",
        "num_respostas", "num_reabertas",
    ]
    existing = [c for c in ticket_cols if c in df.columns]
    n = upsert_batch(sb, "tickets", df[existing].to_dict("records"), "ticket_id")
    log.info(f"tickets       → {n:,} registros")

    # metricas — extrai colunas de métricas do mesmo CSV
    met_cols = [
        "ticket_id",
        "primeira_resposta_min", "primeira_resposta_biz_min",
        "resolucao_min", "resolucao_biz_min",
        "pendencia_min", "pendencia_biz_min",
        "primeira_resposta_h", "resolucao_h", "pendencia_h",
        "num_respostas", "num_reabertas",
    ]
    existing_m = [c for c in met_cols if c in df.columns]
    df_m = df[existing_m].copy()
    for col in ("num_respostas", "num_reabertas"):
        if col in df_m.columns:
            df_m[col] = df_m[col].apply(safe_int)
    n_m = upsert_batch(sb, "metricas", df_m.to_dict("records"), "ticket_id")
    log.info(f"metricas      → {n_m:,} registros")


def load_csat(sb: Client) -> None:
    path = OUTPUT_DIR / "tabela_csat.csv"
    if not path.exists():
        log.warning("tabela_csat.csv não encontrada — pulando")
        return

    df = pd.read_csv(path, low_memory=False)
    df = clean_df(df)

    for col in ("promotor", "detrator"):
        if col in df.columns:
            df[col] = df[col].apply(to_bool)

    n = upsert_batch(sb, "csat", df.to_dict("records"), "csat_id")
    log.info(f"csat          → {n:,} registros")


def load_agentes(sb: Client) -> None:
    path = OUTPUT_DIR / "tabela_agentes.csv"
    if not path.exists():
        log.warning("tabela_agentes.csv não encontrada — pulando")
        return

    df = pd.read_csv(path, low_memory=False)
    df = clean_df(df)

    if "ativo" in df.columns:
        df["ativo"] = df["ativo"].apply(to_bool)

    n = upsert_batch(sb, "agentes", df.to_dict("records"), "agent_id")
    log.info(f"agentes       → {n:,} registros")


def load_grupos(sb: Client) -> None:
    path = OUTPUT_DIR / "tabela_grupos.csv"
    if not path.exists():
        log.warning("tabela_grupos.csv não encontrada — pulando")
        return

    df = pd.read_csv(path, low_memory=False)
    df = clean_df(df)
    n = upsert_batch(sb, "grupos", df.to_dict("records"), "group_id")
    log.info(f"grupos        → {n:,} registros")


def load_comentarios_csat(sb: Client) -> None:
    path = OUTPUT_DIR / "comments_data.json"
    if not path.exists():
        log.warning("comments_data.json não encontrado — pulando")
        return

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # Apaga registros do dia atual antes de reinserir (sem chave única natural)
    sb.table("comentarios_csat").delete().eq("data_carga", TODAY).execute()

    records: list[dict] = []

    # bad / good: [{m, team, t}, ...]
    for tipo in ("bad", "good"):
        for item in data.get(tipo, []):
            records.append({
                "tipo":           tipo,
                "team":           item.get("team"),
                "texto":          item.get("t"),
                "score_numerico": item.get("m"),
                "data_carga":     TODAY,
            })

    # offenders: [[{theme, ex, cnt}, ...], [...], ...]  — índice = mês
    for mes_idx, mes_items in enumerate(data.get("offenders", [])):
        if isinstance(mes_items, list):
            for item in mes_items:
                texto = f"{item.get('theme', '')}: {item.get('ex', '')} ({item.get('cnt', '')})"
                records.append({
                    "tipo":           "offenders",
                    "team":           None,
                    "texto":          texto.strip(),
                    "score_numerico": float(mes_idx),
                    "data_carga":     TODAY,
                })

    n = upsert_batch(sb, "comentarios_csat", records)
    log.info(f"comentarios_csat → {n:,} registros (data_carga={TODAY})")


# ── Ponto de entrada ───────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== db_loader_zendesk iniciado ===")
    sb = get_client()
    load_tickets(sb)
    load_csat(sb)
    load_agentes(sb)
    load_grupos(sb)
    load_comentarios_csat(sb)
    log.info("=== db_loader_zendesk concluído ===")


if __name__ == "__main__":
    main()
