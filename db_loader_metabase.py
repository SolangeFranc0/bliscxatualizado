#!/usr/bin/env python3
"""
db_loader_metabase.py
Carrega dados do Metabase (metabase_data.js) para o Supabase.

Uso:
    python3 db_loader_metabase.py

Pré-requisitos:
    pip install supabase python-dotenv

Variáveis de ambiente (.env):
    SUPABASE_URL=https://auzpgpwvmdhyrkyhzuuf.supabase.co
    SUPABASE_SERVICE_KEY=<service_role_key do painel Supabase>
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
OUTPUT_DIR           = Path(__file__).parent / "output"
BATCH_SIZE           = 500
TODAY                = str(date.today())


# ── Utilitários ────────────────────────────────────────────────────────────────

def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError(
            "SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórios no .env"
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def load_mb_data() -> dict:
    path = OUTPUT_DIR / "metabase_data.js"
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")

    src = path.read_text(encoding="utf-8").strip()
    m = re.search(r"window\.MB_PRELOADED\s*=\s*(\{.*\})\s*;?\s*$", src, re.DOTALL)
    if not m:
        raise ValueError("Formato de metabase_data.js não reconhecido")
    return json.loads(m.group(1))


def to_objects(card: dict) -> list[dict]:
    """Converte estrutura {data: {cols, rows}} para lista de dicts (igual ao JS toObjects)."""
    data = card.get("data", {})
    cols = [
        c.get("name", "") if isinstance(c, dict) else str(c)
        for c in data.get("cols", [])
    ]
    return [dict(zip(cols, row)) for row in data.get("rows", [])]


def get_rows(card: dict) -> list[list]:
    return card.get("data", {}).get("rows", [])


def parse_period(s) -> str | None:
    """Converte YYYY/MM, YYYY-MM ou YYYY-MM-DD para DATE string YYYY-MM-DD."""
    if s is None:
        return None
    s = str(s).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{4})[/-](\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    # Tenta fatiar os primeiros 10 chars (ex: "2026-01-15T...")
    if len(s) >= 10:
        candidate = s[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
            return candidate
    return None


def safe_int(val) -> int | None:
    try:
        f = float(val)
        return None if math.isnan(f) else int(f)
    except (TypeError, ValueError):
        return None


def safe_float(val) -> float | None:
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def delete_today(sb: Client, table: str, col: str = "data_carga") -> None:
    """Remove registros do dia atual antes de reinserir (snapshot diário)."""
    sb.table(table).delete().eq(col, TODAY).execute()


def upsert_batch(sb: Client, table: str, records: list[dict],
                 conflict_col: str | None = None) -> int:
    if not records:
        return 0
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

def load_consultas_status(sb: Client, mb: dict) -> None:
    card = mb.get("consultasPorStatus")
    if not card:
        return
    rows = get_rows(card)
    delete_today(sb, "mb_consultas_status")
    records = []
    for row in rows:
        if len(row) >= 2:
            records.append({
                "data_carga":      TODAY,
                "status_consulta": str(row[0]),
                "qtd_consultas":   safe_int(row[1]),
            })
    n = upsert_batch(sb, "mb_consultas_status", records)
    log.info(f"mb_consultas_status      → {n} registros")


def load_status_temporal(sb: Client, mb: dict) -> None:
    card = mb.get("statusTemporal")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for row in rows:
        if len(row) < 3:
            continue
        period = parse_period(row[0])
        if not period:
            continue
        records.append({
            "periodo":         period,
            "status_consulta": str(row[1]),
            "qtd_consultas":   safe_int(row[2]),
        })
    n = upsert_batch(sb, "mb_status_temporal", records, "periodo,status_consulta")
    log.info(f"mb_status_temporal       → {n} registros")


def load_performance_medicos(sb: Client, mb: dict) -> None:
    card = mb.get("performanceMedicos")
    if not card:
        return
    rows = to_objects(card)
    delete_today(sb, "mb_performance_medicos")

    # Agrega médicos duplicados (múltiplos perfis com mesmo nome)
    agg: dict = {}
    for r in rows:
        nome = (r.get("name") or "").strip()
        if not nome:
            continue
        if nome not in agg:
            agg[nome] = {
                "data_carga":              TODAY,
                "nome":                    nome,
                "status_doctor":           r.get("status_doctor"),
                "created_at":              r.get("created_at"),
                "consultas_finalizadas":   safe_int(r.get("consultas_finalizadas")) or 0,
                "quantidade_orders":       safe_int(r.get("quantidade_orders")) or 0,
                "quantidade_nfs":          safe_int(r.get("quantidade_nfs")) or 0,
                "receita_total_consultas": safe_float(r.get("R$ total consultas")) or 0.0,
                "_csat_sum":               (safe_float(r.get("média_de_avaliações")) or 0.0) * (safe_int(r.get("consultas_finalizadas")) or 1),
                "_nps_sum":                (safe_float(r.get("NPS")) or 0.0) * (safe_int(r.get("consultas_finalizadas")) or 1),
                "_weight":                 safe_int(r.get("consultas_finalizadas")) or 1,
            }
        else:
            a = agg[nome]
            w = safe_int(r.get("consultas_finalizadas")) or 1
            a["consultas_finalizadas"]   += safe_int(r.get("consultas_finalizadas")) or 0
            a["quantidade_orders"]       += safe_int(r.get("quantidade_orders")) or 0
            a["quantidade_nfs"]          += safe_int(r.get("quantidade_nfs")) or 0
            a["receita_total_consultas"] += safe_float(r.get("R$ total consultas")) or 0.0
            a["_csat_sum"]               += (safe_float(r.get("média_de_avaliações")) or 0.0) * w
            a["_nps_sum"]                += (safe_float(r.get("NPS")) or 0.0) * w
            a["_weight"]                 += w

    records = []
    for a in agg.values():
        w = a.pop("_weight")
        csat_sum = a.pop("_csat_sum")
        nps_sum  = a.pop("_nps_sum")
        a["media_avaliacoes"] = round(csat_sum / w, 4) if w else None
        a["nps"]              = round(nps_sum  / w, 4) if w else None
        records.append(a)

    n = upsert_batch(sb, "mb_performance_medicos", records)
    log.info(f"mb_performance_medicos   → {n} registros")


def load_reviews_medicos(sb: Client, mb: dict) -> None:
    card = mb.get("reviewsMedicos")
    if not card:
        return
    rows = to_objects(card)
    # Snapshot completo — limpa antes de reinserir
    sb.table("mb_reviews_medicos").delete().gte("id", 0).execute()
    records = []
    for r in rows:
        data_av = r.get("data da avaliação") or r.get("data_avaliacao")
        nome_doutor = (r.get("nome do doutor") or "").strip() or None
        records.append({
            "data_avaliacao": parse_period(data_av) if data_av else None,
            "nome_doutor":    nome_doutor,
            "nome_paciente":  (r.get("nome do paciente") or "").strip() or None,
            "avaliacao":      safe_float(r.get("avaliação") or r.get("avaliacao")),
            "comentarios":    r.get("comentários") or r.get("comentarios"),
        })
    n = upsert_batch(sb, "mb_reviews_medicos", records)
    log.info(f"mb_reviews_medicos       → {n} registros")


def load_cancelamentos(sb: Client, mb: dict) -> None:
    card = mb.get("cancelamentosPeriodo")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for row in rows:
        period = parse_period(row[0]) if row else None
        if not period:
            continue
        records.append({
            "periodo":               period,
            "consultas_criadas":     safe_int(row[1])   if len(row) > 1 else None,
            "consultas_canceladas":  safe_int(row[2])   if len(row) > 2 else None,
            "percentual_canceladas": safe_float(row[3]) if len(row) > 3 else None,
        })
    n = upsert_batch(sb, "mb_cancelamentos_periodo", records, "periodo")
    log.info(f"mb_cancelamentos_periodo → {n} registros")


def load_nps_periodo(sb: Client, mb: dict) -> None:
    """Combina avaliacaoPeriodo (CSAT) e npsPeriodo num único registro por mês."""
    card_csat = mb.get("avaliacaoPeriodo")
    card_nps  = mb.get("npsPeriodo")

    csat_map: dict[str, dict] = {}
    if card_csat:
        for row in get_rows(card_csat):
            p = parse_period(row[0])
            if p:
                csat_map[p] = {
                    "consultas_criadas": safe_int(row[1])   if len(row) > 1 else None,
                    "media_avaliacoes":  safe_float(row[2]) if len(row) > 2 else None,
                }

    nps_map: dict[str, float | None] = {}
    if card_nps:
        for row in get_rows(card_nps):
            p = parse_period(row[0])
            if p:
                nps_map[p] = safe_float(row[2]) if len(row) > 2 else None

    records = []
    for p in sorted(set(csat_map) | set(nps_map)):
        csat = csat_map.get(p, {})
        records.append({
            "periodo":          p,
            "consultas_criadas": csat.get("consultas_criadas"),
            "media_avaliacoes":  csat.get("media_avaliacoes"),
            "nps":               nps_map.get(p),
        })
    n = upsert_batch(sb, "mb_nps_periodo", records, "periodo")
    log.info(f"mb_nps_periodo           → {n} registros")


def load_tempo_consulta(sb: Client, mb: dict) -> None:
    card = mb.get("tempoMensal")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for row in rows:
        period = parse_period(row[0]) if row else None
        if not period:
            continue
        records.append({
            "periodo":           period,
            "consultas_criadas": safe_int(row[1])   if len(row) > 1 else None,
            "avg_steps_minutos": safe_float(row[2]) if len(row) > 2 else None,
        })
    n = upsert_batch(sb, "mb_tempo_consulta", records, "periodo")
    log.info(f"mb_tempo_consulta        → {n} registros")


def load_coupons(sb: Client, mb: dict) -> None:
    card96  = mb.get("couponsAtivos")
    card108 = mb.get("couponsReceita")
    if not card96:
        return

    # Monta mapa de receita por código (card 108)
    receita_map: dict[str, dict] = {}
    if card108:
        for r in to_objects(card108):
            # Cols esperadas: coupon, valor_total_desc, qtd_usos, val_medio, receita_total, ticket_medio
            code = str(r.get("coupon") or list(r.values())[0] or "").lower()
            if code:
                receita_map[code] = {
                    "receita_total": safe_float(r.get("receita_total") or r.get("valor_total_desc")),
                    "ticket_medio":  safe_float(r.get("ticket_medio") or r.get("val_medio")),
                }

    delete_today(sb, "mb_coupons")
    records = []
    for r in to_objects(card96):
        code = str(r.get("code", "") or "")
        ref  = receita_map.get(code.lower(), {})
        records.append({
            "data_carga":   TODAY,
            "code":         code,
            "type":         r.get("type"),
            "value_type":   r.get("value_type"),
            "value":        safe_float(r.get("value")),
            "max_uses":     safe_int(r.get("max_uses")),
            "uses":         safe_int(r.get("uses")),
            "description":  r.get("description"),
            "active":       r.get("active"),
            "receita_total": ref.get("receita_total"),
            "ticket_medio":  ref.get("ticket_medio"),
        })
    n = upsert_batch(sb, "mb_coupons", records)
    log.info(f"mb_coupons               → {n} registros")


def load_conversao_coupons(sb: Client, mb: dict) -> None:
    card = mb.get("conversaoCoupons")
    if not card:
        return
    rows = to_objects(card)
    delete_today(sb, "mb_conversao_coupons")
    records = []
    for r in rows:
        cupom = r.get("cupom")
        if not cupom:
            continue
        records.append({
            "data_carga":                TODAY,
            "cupom":                     cupom,
            "valor_cupom":               safe_float(r.get("valor_cupom")),
            "tipo_cupom":                r.get("tipo_cupom"),
            "tipo_desconto":             r.get("tipo_de_desconto") or r.get("tipo_desconto"),
            "total_consultas_com_cupom": safe_int(r.get("total_consultas_com_cupom")),
            "consultas_com_pedido":      safe_int(r.get("consultas_com_pedido")),
            "taxa_conversao_pct":        safe_float(r.get("taxa_conversao_pct")),
            "receita_primeiro_pedido":   safe_float(r.get("receita_primeiro_pedido")),
        })
    n = upsert_batch(sb, "mb_conversao_coupons", records)
    log.info(f"mb_conversao_coupons     → {n} registros")


def load_totais_diarios(sb: Client, mb: dict) -> None:
    """KPIs globais: agrega totalConsultas, consultasPorStatus, csatGlobal, tempoAtual."""
    total_consultas = None
    card_total = mb.get("totalConsultas")
    if card_total:
        rows = get_rows(card_total)
        if rows:
            total_consultas = safe_int(rows[0][0])

    finalizadas = None
    card_status = mb.get("consultasPorStatus")
    if card_status:
        for row in get_rows(card_status):
            if len(row) >= 2 and str(row[0]).lower() == "finalizado":
                finalizadas = safe_int(row[1])

    media_av = None
    nps_val  = None
    card_csat = mb.get("csatGlobal")
    if card_csat:
        rows = get_rows(card_csat)
        if rows and len(rows[0]) >= 3:
            media_av = safe_float(rows[0][1])
            nps_val  = safe_float(rows[0][2])

    tempo_medio = None
    card_tempo = mb.get("tempoAtual")
    if card_tempo:
        rows = get_rows(card_tempo)
        if rows:
            tempo_medio = safe_float(rows[0][0])

    record = {
        "data_carga":               TODAY,
        "total_consultas":          total_consultas,
        "consultas_finalizadas":    finalizadas,
        "media_avaliacoes":         media_av,
        "nps":                      nps_val,
        "tempo_medio_consulta_min": tempo_medio,
    }
    sb.table("mb_totais_diarios").upsert(record, on_conflict="data_carga").execute()
    log.info(f"mb_totais_diarios        → 1 registro ({TODAY})")


def load_performance_medicos_mensal(sb: Client, mb: dict) -> None:
    """Carrega performance por médico por mês (card 143 filtrado por data)."""
    card = mb.get("performanceMedicosMensal")
    if not card:
        log.warning("performanceMedicosMensal não encontrado no MB_PRELOADED")
        return

    rows = to_objects(card)
    # Agrega por (periodo, nome_medico) — mesmo médico pode ter dois doctor_ids
    from collections import defaultdict
    agg: dict = defaultdict(lambda: {
        "consultas_criadas": 0, "consultas_finalizadas": 0, "consultas_canceladas": 0,
        "quantidade_orders": 0, "quantidade_nfs": 0, "receita": 0.0,
        "_csat_sum": 0.0, "_nps_sum": 0.0, "_avg_steps_sum": 0.0, "_w": 0,
    })
    for r in rows:
        nome = (r.get("nome_medico") or "").strip()
        periodo = r.get("periodo") or ""
        if not nome or not periodo:
            continue
        key = (periodo, nome)
        a = agg[key]
        cf = safe_int(r.get("consultas_finalizadas")) or 0
        w  = cf or 1
        a["consultas_criadas"]    += safe_int(r.get("consultas_criadas"))    or 0
        a["consultas_finalizadas"]+= cf
        a["consultas_canceladas"] += safe_int(r.get("consultas_canceladas")) or 0
        a["quantidade_orders"]    += safe_int(r.get("quantidade_orders"))    or 0
        a["quantidade_nfs"]       += safe_int(r.get("quantidade_nfs"))       or 0
        a["receita"]              += safe_float(r.get("R$ total consultas")) or 0.0
        a["_csat_sum"]            += (safe_float(r.get("média_de_avaliações")) or 0.0) * w
        a["_nps_sum"]             += (safe_float(r.get("NPS"))                or 0.0) * w
        a["_avg_steps_sum"]       += (safe_float(r.get("avg_steps"))          or 0.0) * w
        a["_w"]                   += w

    records = []
    for (periodo, nome), a in agg.items():
        w = a["_w"] or 1
        records.append({
            "periodo":               periodo,
            "nome_medico":           nome,
            "consultas_criadas":     a["consultas_criadas"],
            "consultas_finalizadas": a["consultas_finalizadas"],
            "consultas_canceladas":  a["consultas_canceladas"],
            "quantidade_orders":     a["quantidade_orders"],
            "quantidade_nfs":        a["quantidade_nfs"],
            "receita":               round(a["receita"], 2),
            "media_avaliacoes":      round(a["_csat_sum"] / w, 4),
            "nps":                   round(a["_nps_sum"]  / w, 4),
            "avg_steps":             round(a["_avg_steps_sum"] / w, 2),
            "data_carga":            TODAY,
        })

    sent = upsert_batch(sb, "mb_performance_medicos_mes", records, "periodo,nome_medico")
    log.info(f"mb_performance_medicos_mes → {sent} registros")


def load_clientes_resumo(sb: Client, mb: dict) -> None:
    """KPIs globais de clientes: total único, taxa de recompra, etc."""
    clientes_unicos = None
    card = mb.get("clientesUnicos")
    if card:
        rows = get_rows(card)
        if rows:
            clientes_unicos = safe_int(rows[0][0])

    recompra_mensal_total = None
    card_mom = mb.get("recompraMoM")
    if card_mom:
        rows = get_rows(card_mom)
        recompra_mensal_total = sum(safe_int(r[1]) or 0 for r in rows if len(r) > 1)

    record = {
        "data_carga":              TODAY,
        "clientes_unicos":         clientes_unicos,
        "total_recompras_periodo": recompra_mensal_total,
    }
    sb.table("mb_clientes_resumo").upsert(record, on_conflict="data_carga").execute()
    log.info(f"mb_clientes_resumo       → 1 registro (únicos={clientes_unicos})")


def load_protocolo_analise(sb: Client, mb: dict) -> None:
    """Análise por protocolo: recompra + pedidos + consultas."""
    card_recompra = mb.get("recompraProtocolo")
    card_pedidos  = mb.get("pedidosProtocolo")
    card_consult  = mb.get("consultasProtocolo")
    if not card_recompra and not card_pedidos:
        return

    # Mapa recompra por protocolo: {name: {qtd_recompras, total_usuarios, pct_recompra}}
    rc_map: dict = {}
    if card_recompra:
        for r in get_rows(card_recompra):
            if len(r) >= 4 and r[0]:
                rc_map[str(r[0])] = {
                    'qtd_recompras':   safe_int(r[1]),
                    'total_usuarios':  safe_int(r[2]),
                    'pct_recompra':    safe_float(r[3]),
                }

    # Mapa pedidos por protocolo: {Protocolo: {soma_receita, qtd_pedidos, ticket_medio}}
    pd_map: dict = {}
    if card_pedidos:
        for r in get_rows(card_pedidos):
            if len(r) >= 4 and r[0]:
                pd_map[str(r[0])] = {
                    'soma_receita': safe_float(r[1]),
                    'qtd_pedidos':  safe_int(r[2]),
                    'ticket_medio': safe_float(r[3]),
                }

    # Mapa consultas por protocolo
    cn_map: dict = {}
    if card_consult:
        for r in get_rows(card_consult):
            if len(r) >= 2 and r[0]:
                cn_map[str(r[0])] = safe_int(r[1])

    all_protos = set(rc_map) | set(pd_map)
    records = []
    for proto in all_protos:
        rc = rc_map.get(proto, {})
        pd = pd_map.get(proto, {})
        records.append({
            "protocolo":      proto,
            "qtd_recompras":  rc.get('qtd_recompras'),
            "total_usuarios": rc.get('total_usuarios'),
            "pct_recompra":   rc.get('pct_recompra'),
            "soma_receita":   pd.get('soma_receita'),
            "qtd_pedidos":    pd.get('qtd_pedidos'),
            "ticket_medio":   pd.get('ticket_medio'),
            "qtd_consultas":  cn_map.get(proto),
            "data_carga":     TODAY,
        })
    n = upsert_batch(sb, "mb_protocolo_analise", records, "protocolo")
    log.info(f"mb_protocolo_analise     → {n} protocolos")


def load_safra_analise(sb: Client, mb: dict) -> None:
    """Análise por safra (coorte) — derivada do card 404 já pré-agregado."""
    card = mb.get("safraAnalise")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for r in rows:
        if len(r) < 5 or not r[0]:
            continue
        records.append({
            "safra":          str(r[0]),
            "total_usuarios": safe_int(r[1]),
            "com_recompra":   safe_int(r[2]),
            "pct_recompra":   safe_float(r[3]),
            "media_pedidos":  safe_float(r[4]),
            "avg_dias_1_2":   safe_float(r[5]) if len(r) > 5 else None,
            "inativos":       safe_int(r[6])   if len(r) > 6 else None,
            "pct_ativou_90d": safe_float(r[7]) if len(r) > 7 else None,
            "data_carga":     TODAY,
        })
    n = upsert_batch(sb, "mb_safra_analise", records, "safra")
    log.info(f"mb_safra_analise         → {n} safras")


def load_tipo_cliente(sb: Client, mb: dict) -> None:
    """Análise por tipo de cliente — derivada do card 404 + card 203."""
    card = mb.get("tipoCliente")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for r in rows:
        if len(r) < 4 or not r[0]:
            continue
        records.append({
            "tipo_cliente":  str(r[0]),
            "qtd_usuarios":  safe_int(r[1]),
            "pct_do_total":  safe_float(r[2]),
            "media_pedidos": safe_float(r[3]),
            "receita_total": safe_float(r[4]) if len(r) > 4 else None,
            "ticket_medio":  safe_float(r[5]) if len(r) > 5 else None,
            "data_carga":    TODAY,
        })
    n = upsert_batch(sb, "mb_tipo_cliente", records, "tipo_cliente")
    log.info(f"mb_tipo_cliente          → {n} tipos")


def load_comportamento_kpis(sb: Client, mb: dict) -> None:
    """KPIs globais de comportamento de compra — derivados do card 404."""
    card = mb.get("comportamentoKpis")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for r in rows:
        if len(r) < 1:
            continue
        records.append({
            "id":              str(r[0]) if r[0] else "global",
            "avg_dias_1_2":    safe_float(r[1]) if len(r) > 1 else None,
            "pct_ativou_90d":  safe_float(r[2]) if len(r) > 2 else None,
            "inativos_global": safe_int(r[3])   if len(r) > 3 else None,
            "data_carga":      TODAY,
        })
    n = upsert_batch(sb, "mb_comportamento_kpis", records, "id")
    log.info(f"mb_comportamento_kpis    → {n} linha(s)")


def load_churn_mensal(sb: Client, mb: dict) -> None:
    """Churn mensal — card 209 agregado por mês."""
    card = mb.get("churnMensal")
    if not card:
        return
    rows = get_rows(card)
    records = []
    for r in rows:
        if len(r) < 2 or not r[0]:
            continue
        records.append({
            "churn_mes":  str(r[0]),
            "qtd_churn":  safe_int(r[1]),
            "data_carga": TODAY,
        })
    n = upsert_batch(sb, "mb_churn_mensal", records, "churn_mes")
    log.info(f"mb_churn_mensal          → {n} meses")


def load_funil_canal(sb: Client, mb: dict) -> None:
    """Funil de performance por canal — card 282."""
    card = mb.get("funilCanal")
    if not card:
        return
    cols = [c["name"] if isinstance(c, dict) else c for c in (card.get("data", {}).get("cols") or [])]
    rows = get_rows(card)
    records = []
    for r in rows:
        if not r or r[0] is None:
            continue
        records.append({
            "canal":        str(r[0]),
            "pre_cadastro": safe_int(r[1])   if len(r) > 1 else None,
            "cadastro":     safe_int(r[2])   if len(r) > 2 else None,
            "consulta":     safe_int(r[4])   if len(r) > 4 else None,
            "pedido":       safe_int(r[5])   if len(r) > 5 else None,
            "recompra":     safe_int(r[6])   if len(r) > 6 else None,
            "pct_consulta": safe_float(r[8]) if len(r) > 8 else None,
            "pct_pedido":   safe_float(r[9]) if len(r) > 9 else None,
            "pct_recompra": safe_float(r[10]) if len(r) > 10 else None,
            "data_carga":   TODAY,
        })
    n = upsert_batch(sb, "mb_funil_canal", records, "canal")
    log.info(f"mb_funil_canal           → {n} canais")


def load_recompra_mensal(sb: Client, mb: dict) -> None:
    """Evolução mensal de recompras — combina recompraMoM + recompraConsulta."""
    card_mom    = mb.get("recompraMoM")
    card_consult = mb.get("recompraConsulta")

    mom_map: dict = {}
    if card_mom:
        for r in get_rows(card_mom):
            p = parse_period(r[0])
            if p:
                mom_map[p] = safe_int(r[1]) if len(r) > 1 else None

    consult_map: dict = {}
    if card_consult:
        for r in get_rows(card_consult):
            p = parse_period(r[0])
            if p:
                consult_map[p] = {
                    'consultas_criadas': safe_int(r[1]) if len(r) > 1 else None,
                    'pedidos_recompra':  safe_int(r[2]) if len(r) > 2 else None,
                    'pedidos':           safe_int(r[3]) if len(r) > 3 else None,
                    'pct_recompra':      safe_float(r[4]) if len(r) > 4 else None,
                }

    all_periods = set(mom_map) | set(consult_map)
    records = []
    for p in sorted(all_periods):
        c = consult_map.get(p, {})
        records.append({
            "periodo":          p,
            "qtd_recompras":    mom_map.get(p),
            "consultas_criadas": c.get('consultas_criadas'),
            "pedidos_recompra":  c.get('pedidos_recompra'),
            "pedidos":           c.get('pedidos'),
            "pct_recompra":      c.get('pct_recompra'),
            "data_carga":        TODAY,
        })
    n = upsert_batch(sb, "mb_recompra_mensal", records, "periodo")
    log.info(f"mb_recompra_mensal       → {n} meses")


# ── Ponto de entrada ───────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== db_loader_metabase iniciado ===")
    sb = get_client()
    mb = load_mb_data()
    log.info(f"Cards disponíveis: {list(mb.keys())}")

    load_consultas_status(sb, mb)
    load_status_temporal(sb, mb)
    load_performance_medicos(sb, mb)
    load_reviews_medicos(sb, mb)
    load_cancelamentos(sb, mb)
    load_nps_periodo(sb, mb)
    load_tempo_consulta(sb, mb)
    load_coupons(sb, mb)
    load_conversao_coupons(sb, mb)
    load_totais_diarios(sb, mb)
    load_performance_medicos_mensal(sb, mb)
    load_clientes_resumo(sb, mb)
    load_protocolo_analise(sb, mb)
    load_safra_analise(sb, mb)
    load_tipo_cliente(sb, mb)
    load_recompra_mensal(sb, mb)
    load_comportamento_kpis(sb, mb)
    load_churn_mensal(sb, mb)
    load_funil_canal(sb, mb)

    log.info("=== db_loader_metabase concluído ===")


if __name__ == "__main__":
    main()
