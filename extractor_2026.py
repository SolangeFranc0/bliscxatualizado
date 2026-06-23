"""
Extração completa Zendesk 2026 — Jan a Mai
Saída: output/tabela_tickets.csv, tabela_metricas.csv, tabela_csat.csv,
       tabela_agentes.csv, tabela_grupos.csv

Correções aplicadas:
  - ts_end inclui o dia completo (23:59:59 UTC) para não cortar Mai/31
  - atendido_por_ia usa group_id em vez de AI_TAGS=[] (que nunca detectava IA)
  - build_csat usa created_at (quando o cliente avaliou) em vez de updated_at
  - tabela_csat enriquecida com group_id e time via join com tabela_tickets
  - fetch_agents inclui admins além de agents
  - deduplica tickets por ticket_id
  - log de auditoria em output/audit_log.txt
"""

from dotenv import load_dotenv
load_dotenv()

import requests
import base64
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from config import (
    BASE_URL, ZENDESK_EMAIL, ZENDESK_TOKEN,
    START_DATE, END_DATE,
    GRUPO_BLIS_SAUDE, GRUPO_BLIS_RESOLVE,
    GRUPO_CLOUD_HUMANS_ID,
    GRUPO_BLIS_SAUDE_ID, GRUPO_BLIS_RESOLVE_ID,
    N2_TAGS, FCR_TAGS,
    MESES_2026,
    CAMPO_MOTIVO_PAI, CAMPO_PERFIL, CAMPOS_SUBMOTIVO,
    MOTIVO_TAG_ID, MOTIVO_SUBMOTIVO_FIELD,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
OUT = Path("output")
OUT.mkdir(exist_ok=True)

BRT = ZoneInfo("America/Sao_Paulo")

# ── Auth ───────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    creds   = f"{ZENDESK_EMAIL}/token:{ZENDESK_TOKEN}"
    encoded = base64.b64encode(creds.encode()).decode()
    return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}


def _get(url: str, params: Optional[dict] = None) -> dict:
    for attempt in range(4):
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                log.warning(f"Rate limit — aguardando {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.error(f"Erro tentativa {attempt + 1}: {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Falha após 4 tentativas: {url}")

# ── Extração ───────────────────────────────────────────────────────────────────

def fetch_groups() -> list[dict]:
    data = _get(f"{BASE_URL}/groups.json")
    groups = data.get("groups", [])
    log.info(f"Grupos encontrados: {[g['name'] for g in groups]}")
    return groups


def fetch_agents() -> list[dict]:
    """Busca agents e admins (ambos atendem tickets)."""
    users: list[dict] = []
    for role in ("agent", "admin"):
        url    = f"{BASE_URL}/users.json"
        params = {"role": role, "per_page": 100}
        while url:
            data = _get(url, params)
            users.extend(data.get("users", []))
            url    = data.get("next_page")
            params = None
    # deduplica por id
    seen: set[int] = set()
    unique = [u for u in users if not (u["id"] in seen or seen.add(u["id"]))]
    log.info(f"Agentes+Admins: {len(unique)}")
    return unique


def fetch_tickets_with_metrics() -> tuple[list[dict], dict[int, dict]]:
    """Incremental export com include=metric_sets — 1000 tickets/página, única passagem.
    ~10x menos chamadas que search/export + bulk separado."""
    ts_start  = int(datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    ts_end_dt = (datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                 + timedelta(days=1))

    url    = f"{BASE_URL}/incremental/tickets.json"
    params = {"start_time": ts_start, "include": "metric_sets"}

    tickets: list[dict]       = []
    metrics_map: dict[int, dict] = {}
    seen_ids: set[int]        = set()
    page = 1

    while url:
        log.info(f"Tickets+métricas — página {page} (acumulado: {len(tickets)})...")
        data = _get(url, params if page == 1 else None)

        for t in data.get("tickets", []):
            created = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            if created < ts_end_dt and t["id"] not in seen_ids:
                seen_ids.add(t["id"])
                ms = t.pop("metric_set", None)
                tickets.append(t)
                if ms:
                    metrics_map[t["id"]] = ms

        if data.get("end_of_stream", False):
            break
        url    = data.get("next_page")
        params = None
        page  += 1
        time.sleep(0.05)

    # Filtra apenas o período configurado (incremental pode trazer tickets antigos)
    tickets = [t for t in tickets
               if START_DATE <= t["created_at"][:10] <= END_DATE]
    log.info(f"Total tickets no período (deduplicados): {len(tickets)}")
    return tickets, metrics_map


def fetch_csat(start: str, end: str) -> list[dict]:
    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = (datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(days=1))

    # API retorna do mais antigo ao mais novo (ascending por created_at)
    # Offset pagination tem limite de 100 páginas → usar cursor pagination (page[size])
    url    = f"{BASE_URL}/satisfaction_ratings.json"
    params = {"page[size]": 100}
    ratings: list[dict] = []

    while url:
        data  = _get(url, params)
        batch = data.get("satisfaction_ratings", [])
        done  = False
        for r in batch:
            created = _dt(r.get("created_at") or r.get("updated_at"))
            if not created:
                continue
            if created >= end_dt:    # mais novo que o intervalo — ignora
                continue
            if created < start_dt:   # mais antigo que o intervalo — para
                done = True
                break
            ratings.append(r)
        if done:
            break
        meta = data.get("meta", {})
        if not meta.get("has_more", False):
            break
        url    = data.get("links", {}).get("next")
        params = None

    log.info(f"CSAT: {len(ratings)} avaliações (bruto)")
    return ratings

# ── Campos customizados ────────────────────────────────────────────────────────

def fetch_field_options(field_ids: list) -> dict:
    """Retorna {field_id_str: {title: str, options: {tag: display_name}}}"""
    result = {}
    for fid in field_ids:
        try:
            data = _get(f"{BASE_URL}/ticket_fields/{fid}.json")
            field = data.get("ticket_field", {})
            opts = {}
            for opt in field.get("custom_field_options", []):
                opts[opt.get("value", "")] = opt.get("name", "")
            result[str(fid)] = {"title": field.get("title", ""), "options": opts}
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"Erro ao buscar campo {fid}: {e}")
    log.info(f"Opções de campos customizados: {len(result)} campos")
    return result


# ── Transformação ──────────────────────────────────────────────────────────────

def _dt(v: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def _to_brt(dt: Optional[datetime]) -> Optional[datetime]:
    """Converte datetime UTC para BRT (UTC-3) para agrupamento correto por mês."""
    return dt.astimezone(BRT) if dt else None


def _has_tag(tags: list, target: list) -> bool:
    return bool(set(tags) & set(target))


def _semana_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


def build_tickets(tickets: list[dict], groups: list[dict]) -> pd.DataFrame:
    group_map = {g["id"]: g["name"] for g in groups}

    def time_label(group_id):
        nome = group_map.get(group_id, "")
        if GRUPO_BLIS_SAUDE.lower()   in nome.lower(): return "Blis Saúde"
        if GRUPO_BLIS_RESOLVE.lower() in nome.lower(): return "Blis Resolve"
        return "Outros"

    rows = []
    for t in tickets:
        tags     = t.get("tags", [])
        criado   = _dt(t.get("created_at"))
        criado_b = _to_brt(criado)
        group_id = t.get("group_id")

        # Extrair custom fields: motivo pai, submotivo do campo correto, perfil
        cf_map = {f["id"]: f.get("value") for f in t.get("custom_fields", [])}
        motivo_tag = cf_map.get(CAMPO_MOTIVO_PAI)
        motivo_id  = MOTIVO_TAG_ID.get(motivo_tag) if motivo_tag else None

        # Submotivo: usa o campo específico do motivo (evita pegar campo errado)
        submotivo_tag = None
        submotivo_field_id = None
        if motivo_id and motivo_id in MOTIVO_SUBMOTIVO_FIELD:
            correct_fid = MOTIVO_SUBMOTIVO_FIELD[motivo_id]
            v = cf_map.get(correct_fid)
            if v:
                submotivo_tag = v
                submotivo_field_id = correct_fid
        elif motivo_id is None:
            # Sem motivo: tenta qualquer campo de submotivo como fallback
            for fid in CAMPOS_SUBMOTIVO:
                v = cf_map.get(fid)
                if v:
                    submotivo_tag = v
                    submotivo_field_id = fid
                    break

        perfil_tag = cf_map.get(CAMPO_PERFIL)

        rows.append({
            "ticket_id":          t["id"],
            "criado_em":          criado,
            "criado_em_brt":      criado_b,
            "atualizado_em":      _dt(t.get("updated_at")),
            "status":             t.get("status"),
            "prioridade":         t.get("priority") or "none",
            "canal":              t.get("via", {}).get("channel"),
            "assunto":            t.get("subject"),
            "tags":               "|".join(tags),
            "group_id":           group_id,
            "nome_grupo":         group_map.get(group_id, ""),
            "time":               time_label(group_id),
            "assignee_id":        t.get("assignee_id"),
            "requester_id":       t.get("requester_id"),
            "organization_id":    t.get("organization_id"),
            "atendido_por_ia":    group_id == GRUPO_CLOUD_HUMANS_ID,
            "transferido_n2":     _has_tag(tags, N2_TAGS),
            "resolvido_fcr":      _has_tag(tags, FCR_TAGS),
            # Dimensões temporais em BRT
            "ano_mes":            criado_b.strftime("%Y-%m") if criado_b else None,
            "nome_mes":           MESES_2026.get(criado_b.strftime("%Y-%m"), "") if criado_b else None,
            "semana_iso":         _semana_iso(criado_b),
            "dia_semana":         criado_b.strftime("%A") if criado_b else None,
            # Campos de motivo/perfil (novos)
            "motivo":             motivo_id,
            "motivo_tag":         motivo_tag,
            "submotivo_tag":      submotivo_tag,
            "submotivo_field_id": submotivo_field_id,
            "perfil":             perfil_tag,
        })

    df = pd.DataFrame(rows)
    df["criado_em"]     = pd.to_datetime(df["criado_em"],     utc=True)
    df["atualizado_em"] = pd.to_datetime(df["atualizado_em"], utc=True)
    return df


def build_metrics(metrics_map: dict[int, dict]) -> pd.DataFrame:
    rows = []
    for tid, m in metrics_map.items():
        rc = (m.get("reply_time_in_minutes")              or {}).get("calendar")
        rb = (m.get("reply_time_in_minutes")              or {}).get("business")
        fc = (m.get("full_resolution_time_in_minutes")    or {}).get("calendar")
        fb = (m.get("full_resolution_time_in_minutes")    or {}).get("business")
        pc = (m.get("requester_wait_time_in_minutes")     or {}).get("calendar")
        pb = (m.get("requester_wait_time_in_minutes")     or {}).get("business")
        rows.append({
            "ticket_id":               tid,
            "primeira_resposta_min":   rc,
            "primeira_resposta_biz_min": rb,
            "resolucao_min":           fc,
            "resolucao_biz_min":       fb,
            "pendencia_min":           pc,
            "pendencia_biz_min":       pb,
            "primeira_resposta_h":     round(rc / 60, 2) if rc else None,
            "resolucao_h":             round(fc / 60, 2) if fc else None,
            "pendencia_h":             round(pc / 60, 2) if pc else None,
            "num_respostas":           m.get("replies"),
            "num_reabertas":           m.get("reopens"),
        })
    return pd.DataFrame(rows)


def build_csat(ratings: list[dict], df_tickets: pd.DataFrame) -> pd.DataFrame:
    # Usa o group_id da própria avaliação (mesmo critério da Zendesk nativa),
    # não o group atual do ticket (que pode ter mudado após a avaliação).
    score_num = {"good": 5, "bad": 1, "offered": None}

    def _time_from_group(gid):
        if gid == GRUPO_BLIS_SAUDE_ID:   return "Blis Saúde"
        if gid == GRUPO_BLIS_RESOLVE_ID:  return "Blis Resolve"
        if gid == GRUPO_CLOUD_HUMANS_ID:  return "IA"
        return "Outros"

    rows = []
    for r in ratings:
        raw   = r.get("score")
        score = score_num.get(raw)
        dt    = _dt(r.get("created_at") or r.get("updated_at"))
        dt_b  = _to_brt(dt)
        gid   = r.get("group_id")
        rows.append({
            "csat_id":      r["id"],
            "ticket_id":    r.get("ticket_id"),
            "assignee_id":  r.get("assignee_id"),
            "score_raw":    raw,
            "score":        score,
            "comentario":   r.get("comment"),
            "avaliado_em":  dt,
            "ano_mes":      dt_b.strftime("%Y-%m") if dt_b else None,
            "semana_iso":   _semana_iso(dt_b),
            "promotor":     score == 5,
            "detrator":     score == 1,
            "group_id":     gid,
            "nome_grupo":   _time_from_group(gid),
            "time":         _time_from_group(gid),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["avaliado_em"] = pd.to_datetime(df["avaliado_em"], utc=True)
    return df


def build_agents(agents: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "agent_id": a["id"],
        "nome":     a.get("name"),
        "email":    a.get("email"),
        "role":     a.get("role"),
        "ativo":    not a.get("suspended", False),
    } for a in agents])


def build_groups(groups: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "group_id":   g["id"],
        "nome_grupo": g.get("name"),
    } for g in groups])


def write_audit_log(df_tickets: pd.DataFrame, df_csat: pd.DataFrame) -> None:
    lines = ["=== AUDIT LOG — Extração Zendesk 2026 ===", f"Gerado em: {datetime.now().isoformat()}", ""]

    lines.append("--- Tickets por time e mês ---")
    if not df_tickets.empty:
        pivot = df_tickets.groupby(["time", "ano_mes"])["ticket_id"].count().unstack(fill_value=0)
        lines.append(pivot.to_string())
    lines.append("")

    lines.append("--- CSAT: contagem por time e mês (positivas / negativas / offered) ---")
    if not df_csat.empty and "time" in df_csat.columns:
        for team in df_csat["time"].dropna().unique():
            sub = df_csat[df_csat["time"] == team]
            lines.append(f"\n{team}:")
            pivot_c = sub.groupby(["ano_mes", "score_raw"])["csat_id"].count().unstack(fill_value=0)
            lines.append(pivot_c.to_string())
            for mes, g in sub.groupby("ano_mes"):
                pos = (g["score_raw"] == "good").sum()
                neg = (g["score_raw"] == "bad").sum()
                pct = round(pos / (pos + neg) * 100, 1) if (pos + neg) > 0 else None
                lines.append(f"  {mes}: {pos} pos / {neg} neg → CSAT {pct}%")
    lines.append("")

    lines.append("--- Tickets IA (group_id = GRUPO_CLOUD_HUMANS_ID) ---")
    if not df_tickets.empty:
        ia = df_tickets[df_tickets["atendido_por_ia"] == True]
        lines.append(f"Total IA: {len(ia)}")
        if not ia.empty:
            lines.append(ia.groupby("ano_mes")["ticket_id"].count().to_string())

    path = OUT / "audit_log.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"Audit log salvo: {path}")

# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    import json as _json
    log.info("=== Extração Zendesk 2026 ===")

    groups               = fetch_groups()
    agents               = fetch_agents()
    tickets, metrics_map = fetch_tickets_with_metrics()
    ratings              = fetch_csat(START_DATE, END_DATE)

    # Buscar opções dos campos customizados (submotivos e perfil) para nomes de exibição
    all_custom_field_ids = [CAMPO_MOTIVO_PAI, CAMPO_PERFIL] + CAMPOS_SUBMOTIVO
    opcoes = fetch_field_options(all_custom_field_ids)
    opcoes_path = OUT / "campo_opcoes.json"
    opcoes_path.write_text(_json.dumps(opcoes, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Opções salvas: {opcoes_path}")

    df_tickets = build_tickets(tickets, groups)
    df_metrics = build_metrics(metrics_map)
    df_agents  = build_agents(agents)
    df_groups  = build_groups(groups)

    # Enrich tickets com nome do agente
    df_tickets = df_tickets.merge(
        df_agents[["agent_id", "nome"]].rename(columns={"agent_id": "assignee_id", "nome": "nome_agente"}),
        on="assignee_id", how="left"
    )

    df_csat = build_csat(ratings, df_tickets)

    # Deduplica: keep only latest rating per ticket (Zendesk counts latest per ticket)
    if "avaliado_em" in df_csat.columns:
        df_csat["avaliado_em"] = pd.to_datetime(df_csat["avaliado_em"], utc=True, errors="coerce")
        before = len(df_csat)
        df_csat = (df_csat.sort_values("avaliado_em", ascending=False)
                           .drop_duplicates("ticket_id", keep="first")
                           .reset_index(drop=True))
        if before != len(df_csat):
            log.info(f"CSAT dedup: {before - len(df_csat)} duplicatas removidas ({before} → {len(df_csat)})")

    # Merge métricas para updater calcular TMA/TMR/TMP
    df_full = df_tickets.merge(df_metrics, on="ticket_id", how="left")

    for name, df in [
        ("tabela_tickets",  df_full),
        ("tabela_metricas", df_metrics),
        ("tabela_csat",     df_csat),
        ("tabela_agentes",  df_agents),
        ("tabela_grupos",   df_groups),
    ]:
        path = OUT / f"{name}.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        log.info(f"Salvo: {path} ({len(df)} linhas)")

    write_audit_log(df_tickets, df_csat)

    print("\n========== RESUMO ==========")
    print(f"Tickets      : {len(df_tickets)}")
    print(f"Com métricas : {len(df_metrics)}")
    print(f"CSAT         : {len(df_csat)}")
    print(f"Grupos       : {[g['name'] for g in groups]}")
    print(f"\nTimes mapeados:")
    print(df_tickets.groupby("time")["ticket_id"].count().to_string())
    print(f"\nIA (Cloud Humans) por mês:")
    ia = df_tickets[df_tickets["atendido_por_ia"] == True]
    if not ia.empty:
        print(ia.groupby("ano_mes")["ticket_id"].count().to_string())
    else:
        print("  Nenhum ticket IA detectado — verifique GRUPO_CLOUD_HUMANS_ID")
    if not df_csat.empty and "time" in df_csat.columns:
        print(f"\nCSAT por time:")
        for team, g in df_csat[df_csat["score_raw"].isin(["good","bad"])].groupby("time"):
            pos = (g["score_raw"] == "good").sum()
            neg = (g["score_raw"] == "bad").sum()
            pct = round(pos / (pos + neg) * 100, 1) if (pos + neg) > 0 else None
            print(f"  {team}: {pos} pos / {neg} neg -> {pct}%")


if __name__ == "__main__":
    run()
