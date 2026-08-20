"""
updater.py — Atualização diária do dashboard_2026.html a partir dos dados do Zendesk.

Fluxo (3 funções principais):
  1. collect_and_build(save_csv=True)  — Zendesk + processamento + HTML + Supabase tickets/csat
  2. sync_metabase(save_js=True)       — Metabase → Supabase (fallback JS mantido)
  3. upload_ftp()                      — envia arquivos para Hostinger
"""

from dotenv import load_dotenv
load_dotenv()

import os, re, json, shutil, logging, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from ftplib import FTP, all_errors as FTP_ERRORS

import pandas as pd

BASE           = Path(__file__).parent
OUT            = BASE / "output"
DASHBOARD      = BASE / "dashboard_2026.html"
DASHBOARD_COPY = BASE / "dashboard.html"      # cópia servida pelo portal via iframe
PORTAL         = BASE / "cx-portal.html"
LOG_FILE       = OUT  / "update_log.txt"
ZD_SYNC_FILE   = OUT  / "last_zendesk_sync.txt"   # cursor incremental Zendesk
BACKUP         = BASE / "dashboard_2026.backup.html"
BRT       = ZoneInfo("America/Sao_Paulo")

# ── FTP Hostinger ─────────────────────────────────────────────────────────────
FTP_HOST          = os.getenv("FTP_HOST",   "")
FTP_USER          = os.getenv("FTP_USER",   "")
FTP_PASS          = os.getenv("FTP_PASS",   "")
_FTP_BASE              = "/domains/mediumturquoise-fish-127944.hostingersite.com/public_html/dash"
FTP_REMOTE_DASH        = f"{_FTP_BASE}/dashboard.html"
FTP_REMOTE_PORTAL      = f"{_FTP_BASE}/index.html"
FTP_REMOTE_MB_SERVICE  = f"{_FTP_BASE}/services/metabase.js"
FTP_REMOTE_MB_DATA     = f"{_FTP_BASE}/data/metabase_data.js"
MB_SERVICE             = BASE / "services" / "metabase.js"
MB_DATA_JS             = OUT  / "metabase_data.js"

# ── Metabase ──────────────────────────────────────────────────────────────────
METABASE_URL   = os.getenv("METABASE_URL",      "https://blis-metabase.azurewebsites.net")
METABASE_EMAIL = os.getenv("METABASE_EMAIL",    "")
METABASE_PASS  = os.getenv("METABASE_PASSWORD", "")
MB_CARD_IDS = {
    "totalConsultas":        33,
    "consultasPorStatus":   130,
    "statusTemporal":       515,
    "csatGlobal":            95,
    "performanceMedicos":    84,
    "reviewsMedicos":        85,
    "couponsAtivos":         96,
    "couponsReceita":       108,
    "conversaoCoupons":     398,
    "custoCupomConsulta":   531,
    "cancelamentosPeriodo": 494,
    "avaliacaoPeriodo":     496,
    "npsPeriodo":           497,
    "consultasPorMedico":   402,
    "tempoMensal":          498,
    "tempoAtual":           503,
    "clientesUnicos":       462,
    "recompraProtocolo":    264,
    # card 445 (pedidosProtocolo) buscado separadamente com timeout maior
    "consultasProtocolo":    41,
    "recompraMoM":          110,
    "cohortPedidos":        201,
    "recompraConsulta":     499,
    "funilCanal":           282,
}
MB_ROW_LIMITS = {"statusTemporal": 600, "reviewsMedicos": 2000, "conversaoCoupons": 200, "cancelamentosPeriodo": 200}

OUT.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(__import__("sys").stdout),
    ],
)
log = logging.getLogger(__name__)

# MONTH_IDX e WEEK_TO_MONTH gerados dinamicamente — funcionam em qualquer mês futuro.
def _build_month_idx() -> dict:
    import calendar as _cal
    from config import START_DATE
    start = datetime.strptime(START_DATE, "%Y-%m-%d").date()
    today = datetime.now(BRT).date()
    idx, y, m, i = {}, start.year, start.month, 0
    while (y, m) <= (today.year, today.month):
        idx[f"{y}-{m:02d}"] = i
        i += 1
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return idx

def _build_week_to_month(month_idx: dict) -> dict:
    """Associa cada semana ISO ao mês em que tem mais dias."""
    w2m: dict[int, int] = {}
    for ym, mi in month_idx.items():
        y, m = int(ym[:4]), int(ym[5:])
        import calendar as _cal
        _, days_in = _cal.monthrange(y, m)
        for day in range(1, days_in + 1):
            iso_w = datetime(y, m, day).isocalendar()[1]
            w2m.setdefault(iso_w, mi)
    return w2m

MONTH_IDX     = _build_month_idx()
N_MONTHS      = len(MONTH_IDX)
WEEK_TO_MONTH = _build_week_to_month(MONTH_IDX)

# ── Motivos / Perfis ──────────────────────────────────────────────────────────

MOTIVO_SUBMOTIVO_FIELD = {
    "appblis":   43754976302099,
    "cadastros": 44807136133907,
    "medicos":   43771301635091,
    "docs":      43771237175955,
    "logistica": 43754883922323,
    "pagamento": 43754737472019,
    "estoque":   43771177081747,
    "receitas":  43754673247379,
    "cancela":   48166687223315,
}

MOTIVO_LABELS = {
    "appblis":   "AppBlis",
    "cadastros": "Cadastros",
    "medicos":   "Blis Médicos",
    "docs":      "Documentação",
    "logistica": "Logística",
    "pagamento": "Pagamentos",
    "estoque":   "Produtos/Estoque",
    "receitas":  "Receitas",
    "cancela":   "Cancelar Tratamento",
}

PERFIL_LABELS = {
    "não_baixou_app__sem_login":                               "Não Baixou App",
    "perfil_paciente":                                         "Fez Login",
    "perfil_nao_paciente":                                     "Fez Cadastro",
    "perfil_medico":                                           "Fez Anamnese",
    "fez_consulta__não_comprou":                               "Fez Consulta",
    "fez_primeira_compra_há_menos_de_30_dias":                 "1ª Compra <30d",
    "fez_primeira_compra_há_mais_de_30_dias_e_não_recomprou_": "1ª Compra >30d",
    "fez_segunda_recompra_ou_mais":                            "2ª+ Recompra",
    "medicos":                                                 "Médicos",
    "pergunta":                                                "Pergunta",
}

PERFIL_ORDER = [
    "não_baixou_app__sem_login",
    "perfil_paciente",
    "perfil_nao_paciente",
    "perfil_medico",
    "fez_consulta__não_comprou",
    "fez_primeira_compra_há_menos_de_30_dias",
    "fez_primeira_compra_há_mais_de_30_dias_e_não_recomprou_",
    "fez_segunda_recompra_ou_mais",
    "medicos",
    "pergunta",
]

# ── Validação ─────────────────────────────────────────────────────────────────

def validate_csv(df: pd.DataFrame) -> list[str]:
    issues = []
    dupes = df.duplicated("ticket_id").sum()
    if dupes:
        issues.append(f"DUPLICATAS: {dupes} ticket_ids duplicados")
    nulls = df["ticket_id"].isnull().sum()
    if nulls:
        issues.append(f"NULOS: {nulls} ticket_ids nulos")
    for col in ("status","group_id","ano_mes","atendido_por_ia"):
        if col not in df.columns:
            issues.append(f"COLUNA AUSENTE: {col}")
    return issues

# ── Classificação de time ─────────────────────────────────────────────────────

TEAM_OF_TIME = {
    "Blis Saúde": "saude", "Blis Resolve": "resolve",
    "Blis Logística": "logistica", "Outros": "resolve",
    "IA": "ia", "Cloud Humans": "ia",
}
_CLOUD_HUMANS_GID = 50304023554451  # config.GRUPO_CLOUD_HUMANS_ID

def _team(row) -> str:
    # 1. flag explícita
    v = row.get("atendido_por_ia")
    if v is True or str(v).lower() == "true":
        return "ia"
    # 2. group_id direto (cobre histórico sem atendido_por_ia setado)
    try:
        if int(str(row.get("group_id") or 0).split(".")[0]) == _CLOUD_HUMANS_GID:
            return "ia"
    except (ValueError, TypeError):
        pass
    # 3. nome do grupo
    nome = str(row.get("nome_grupo", ""))
    if "Cloud Humans" in nome or "CloudHumans" in nome:
        return "ia"
    if "Logística" in nome or "Logistica" in nome:
        return "logistica"
    return TEAM_OF_TIME.get(str(row.get("time", "")), "resolve")

# ── Construção dos blocos ─────────────────────────────────────────────────────

def build_tickets(df: pd.DataFrame) -> dict:
    out = {t: [0]*N_MONTHS for t in ("ia","saude","resolve","logistica")}
    for _, r in df.iterrows():
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is not None:
            out[_team(r)][m] += 1
    return out

def build_status(df: pd.DataFrame) -> dict:
    out = {t: {"closed":0,"solved":0,"open":0,"pending":0,"new_":0}
           for t in ("ia","saude","resolve","logistica")}
    smap = {"closed":"closed","solved":"solved","open":"open",
            "pending":"pending","new":"new_","hold":"open","deleted":"closed"}
    for _, r in df.iterrows():
        if MONTH_IDX.get(str(r.get("ano_mes",""))) is None:
            continue
        key = smap.get(str(r.get("status","") or "").lower())
        if key:
            out[_team(r)][key] += 1
    return out

def build_channels(df: pd.DataFrame) -> dict:
    CHAN_LABEL = {
        "whatsapp":"WhatsApp","web":"Web/E-mail","email":"Web/E-mail",
        "native_messaging":"Native Msg","facebook":"Facebook",
    }
    out = {"ia":{},"saude":{},"resolve":{},"logistica":{}}
    for _, r in df.iterrows():
        if MONTH_IDX.get(str(r.get("ano_mes",""))) is None:
            continue
        t  = _team(r)
        ch = str(r.get("canal","desconhecido")).lower().replace(" ","_")
        lbl = CHAN_LABEL.get(ch, str(r.get("canal","Outros")))
        out[t][lbl] = out[t].get(lbl, 0) + 1
    return out

def build_channels_monthly(df: pd.DataFrame) -> dict:
    CHAN_LABEL = {
        "whatsapp":"WhatsApp","web":"Web/E-mail","email":"Web/E-mail",
        "native_messaging":"Native Msg","facebook":"Facebook",
    }
    out = {t: [{} for _ in range(N_MONTHS)] for t in ("ia","saude","resolve","logistica")}
    for _, r in df.iterrows():
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is None:
            continue
        t  = _team(r)
        ch = str(r.get("canal","desconhecido")).lower().replace(" ","_")
        lbl = CHAN_LABEL.get(ch, str(r.get("canal","Outros")))
        out[t][m][lbl] = out[t][m].get(lbl, 0) + 1
    return out

def build_semanas(df: pd.DataFrame) -> dict:
    """Agrupa tickets por semana ISO a partir de criado_em_brt."""
    col = "criado_em_brt" if "criado_em_brt" in df.columns else "criado_em"
    teams = ("ia","saude","resolve","logistica")

    # Filtra apenas tickets com ano_mes válido — evita contar spillover BRT
    valid = df["ano_mes"].map(lambda x: str(x) in MONTH_IDX)
    df2 = df[valid].copy()
    df2["_dt"] = pd.to_datetime(df2[col], utc=True, errors="coerce")
    df2["_dt"] = df2["_dt"].dt.tz_convert(BRT)
    df2["_wk"] = df2["_dt"].apply(
        lambda d: d.isocalendar().week if pd.notna(d) else None
    )
    df2["_team"] = df2.apply(_team, axis=1)

    # Todas as semanas no período
    all_weeks = sorted(
        w for w in df2["_wk"].dropna().unique()
        if int(w) in WEEK_TO_MONTH
    )

    labels  = [f"S{int(w):02d}" for w in all_weeks]
    mes_idx = [WEEK_TO_MONTH[int(w)] for w in all_weeks]

    # Data de início de cada semana (segunda-feira)
    def week_start(w):
        jan4 = datetime(2026, 1, 4)  # 4 jan 2026 = semana 1
        delta = timedelta(weeks=int(w)-1)
        return (jan4 - timedelta(days=jan4.weekday()) + delta).strftime("%d/%m")

    datas = [week_start(w) for w in all_weeks]

    weekly = {t: [] for t in teams}
    for w in all_weeks:
        sub = df2[df2["_wk"] == w]
        for t in teams:
            weekly[t].append(int((sub["_team"] == t).sum()))

    return {
        "labels":    labels,
        "datas":     datas,
        "mesIdx":    mes_idx,
        "ia":        weekly["ia"],
        "saude":     weekly["saude"],
        "resolve":   weekly["resolve"],
        "logistica": weekly["logistica"],
    }

_CSAT_GROUP_MAP = {
    50304023554451: "ia",
    42056691282323: "resolve",
    43771604769299: "saude",
}

def build_csat(df_c: pd.DataFrame, df_t: pd.DataFrame) -> dict:
    out = {t: {"good":[0]*N_MONTHS,"bad":[0]*N_MONTHS}
           for t in ("ia","saude","resolve","logistica")}
    # Usa o campo `time` do CSAT como fonte primária.
    # Fallback 1: group_id direto (cobre registros com time ausente/divergente).
    # Fallback 2: cross-referência com tabela_tickets.
    ia_ticket_ids: set = set()
    logistica_ticket_ids: set = set()
    for _, r in df_t.iterrows():
        if pd.isna(r.get("ticket_id")):
            continue
        team = _team(r)
        if team == "ia":
            ia_ticket_ids.add(str(r["ticket_id"]))
        elif team == "logistica":
            logistica_ticket_ids.add(str(r["ticket_id"]))
    for _, r in df_c.iterrows():
        score = str(r.get("score_raw",""))
        if score not in ("good","bad"):
            continue
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is None:
            continue
        tid = str(r.get("ticket_id",""))
        # Prioridade 1: ticket_id lookup — se o ticket passou pela IA conta como IA
        # (mesmo que a avaliação chegou depois de transferência — igual ao Zendesk)
        if tid in ia_ticket_ids:
            out["ia"][score][m] += 1
            continue
        if tid in logistica_ticket_ids:
            out["logistica"][score][m] += 1
            continue
        time_val = str(r.get("time",""))
        if "Saúde" in time_val or "Saude" in time_val or time_val == "saude":
            out["saude"][score][m] += 1
        elif "Resolve" in time_val or time_val == "resolve":
            out["resolve"][score][m] += 1
        elif time_val == "IA" or "Cloud Humans" in time_val or time_val == "ia":
            out["ia"][score][m] += 1
        elif "Logística" in time_val or "Logistica" in time_val or time_val == "logistica":
            out["logistica"][score][m] += 1
        else:
            # Fallback: group_id
            try:
                gid = int(str(r.get("group_id") or 0).split(".")[0])
                team_by_gid = _CSAT_GROUP_MAP.get(gid)
            except (ValueError, TypeError):
                team_by_gid = None
            if team_by_gid:
                out[team_by_gid][score][m] += 1
    return out

# ── Motivos / Submotivos / Perfis ────────────────────────────────────────────

def _clean_tag(tag: str) -> str:
    return " ".join(w.capitalize() for w in tag.split("_") if w)

def load_opcoes() -> dict:
    path = OUT / "campo_opcoes.json"
    if not path.exists():
        return {}
    try:
        import json as _json
        raw = _json.loads(path.read_text(encoding="utf-8"))
        tag_names: dict[str, str] = {}
        for fid, info in raw.items():
            for tag, name in info.get("options", {}).items():
                if tag:
                    tag_names[tag] = name
        log.info(f"campo_opcoes: {len(tag_names)} tags carregadas")
        return tag_names
    except Exception as e:
        log.warning(f"campo_opcoes.json invalido: {e}")
        return {}

def build_status_monthly(df: pd.DataFrame) -> dict:
    """Status dos tickets agrupado por mês — permite filtrar 'Resolvidos' por período."""
    smap = {"closed":"closed","solved":"solved","open":"open",
            "pending":"pending","new":"new_","hold":"open","deleted":"closed"}
    empty = lambda: {"closed":0,"solved":0,"open":0,"pending":0,"new_":0}
    out = {t: [empty() for _ in range(N_MONTHS)] for t in ("ia","saude","resolve","logistica")}
    for _, r in df.iterrows():
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is None:
            continue
        key = smap.get(str(r.get("status","") or "").lower())
        if key:
            out[_team(r)][m][key] += 1
    return out

def build_n2(df: pd.DataFrame) -> dict:
    """Computa escalamentos N2 por time e por mês (tag transferido_n2=True)."""
    out = {t: [0]*N_MONTHS for t in ("ia","saude","resolve","logistica")}
    for _, r in df.iterrows():
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is None:
            continue
        v = r.get("transferido_n2")
        if v is True or str(v).lower() == "true":
            out[_team(r)][m] += 1
    return out

def build_motivos_data(df_t: pd.DataFrame) -> list:
    if "motivo" not in df_t.columns:
        log.warning("Coluna 'motivo' ausente — motivos nao atualizados")
        return []
    df = df_t.copy()
    df["_team"] = df.apply(_team, axis=1)
    df["_m"]    = df["ano_mes"].map(MONTH_IDX)
    df = df.dropna(subset=["_m", "motivo"])
    df["_m"] = df["_m"].astype(int)

    motivos = []
    for motivo_id, label in MOTIVO_LABELS.items():
        sub = df[df["motivo"] == motivo_id]
        monthly = [0]*N_MONTHS; ia=[0]*N_MONTHS; saude=[0]*N_MONTHS; resolve=[0]*N_MONTHS; logistica=[0]*N_MONTHS
        for (team, m), grp in sub.groupby(["_team","_m"]):
            n = len(grp)
            monthly[m] += n
            if team == "ia":              ia[m]          += n
            elif team == "saude":         saude[m]       += n
            elif team == "logistica":     logistica[m]   += n
            else:                         resolve[m]     += n
        motivos.append({"id":motivo_id,"nome":label,"monthly":monthly,"resolve":resolve,"saude":saude,"ia":ia,"logistica":logistica})
    return motivos

def build_sub_data(df_t: pd.DataFrame, tag_names: dict) -> dict:
    if "motivo" not in df_t.columns or "submotivo_tag" not in df_t.columns:
        return {}
    df = df_t.copy()
    df["_m"] = df["ano_mes"].map(MONTH_IDX)
    df = df.dropna(subset=["_m","motivo","submotivo_tag"])
    df["_m"] = df["_m"].astype(int)
    # submotivo_field_id é salvo como float no CSV — converter para comparação
    if "submotivo_field_id" in df.columns:
        df["_fid"] = pd.to_numeric(df["submotivo_field_id"], errors="coerce")

    sub = {}
    for motivo_id in MOTIVO_LABELS:
        sub_df = df[df["motivo"] == motivo_id]
        # Filtra apenas submotivos do campo correto para este motivo
        correct_fid = MOTIVO_SUBMOTIVO_FIELD.get(motivo_id)
        if correct_fid and "_fid" in sub_df.columns:
            sub_df = sub_df[sub_df["_fid"] == float(correct_fid)]
        entries = []
        for sub_tag, grp in sub_df.groupby("submotivo_tag"):
            monthly = [0]*N_MONTHS
            for m, cnt in grp.groupby("_m").size().items():
                monthly[int(m)] = int(cnt)
            name = tag_names.get(str(sub_tag), _clean_tag(str(sub_tag)))
            entries.append({"nome": name, "monthly": monthly})
        entries.sort(key=lambda e: -sum(e["monthly"]))
        sub[motivo_id] = entries[:8]
    return sub

def build_perfis_data(df_t: pd.DataFrame) -> dict:
    if "perfil" not in df_t.columns:
        log.warning("Coluna 'perfil' ausente — perfis nao atualizados")
        return {}
    df = df_t.copy()
    df["_team"] = df.apply(_team, axis=1)

    labels: list[str] = []; ids: list[str] = []
    ia_c: list[int] = []; saude_c: list[int] = []; resolve_c: list[int] = []; logistica_c: list[int] = []
    motivos_per_perfil: dict[str, list[str]] = {}

    counts_per_perfil: dict[str, dict] = {}

    for perfil_tag in PERFIL_ORDER:
        sub = df[df["perfil"] == perfil_tag]
        if sub.empty:
            continue
        labels.append(PERFIL_LABELS.get(perfil_tag, perfil_tag))
        ids.append(perfil_tag)
        tc = sub["_team"].value_counts()
        ia_c.append(int(tc.get("ia", 0)))
        saude_c.append(int(tc.get("saude", 0)))
        resolve_c.append(int(tc.get("resolve", 0)))
        logistica_c.append(int(tc.get("logistica", 0)))
        if "motivo" in sub.columns:
            vc = sub[sub["motivo"].notna()]["motivo"].value_counts()
            motivos_per_perfil[perfil_tag] = [str(m) for m in vc[vc > 0].index.tolist()]
            # Contagens por motivo × time para este perfil
            counts = {}
            for mot_id in MOTIVO_LABELS:
                msub = sub[sub["motivo"] == mot_id]
                if msub.empty:
                    continue
                mtc = msub["_team"].value_counts()
                counts[mot_id] = {
                    "total":     len(msub),
                    "ia":        int(mtc.get("ia", 0)),
                    "saude":     int(mtc.get("saude", 0)),
                    "resolve":   int(mtc.get("resolve", 0)),
                    "logistica": int(mtc.get("logistica", 0)),
                }
            counts_per_perfil[perfil_tag] = counts
        else:
            motivos_per_perfil[perfil_tag] = []

    log.info(f"Perfis encontrados: {labels}")
    return {"labels": labels, "ids": ids, "ia": ia_c, "saude": saude_c, "resolve": resolve_c, "logistica": logistica_c,
            "motivos": motivos_per_perfil, "counts": counts_per_perfil}


# ── Validação cruzada ─────────────────────────────────────────────────────────

def cross_check(tickets: dict, status: dict, channels: dict,
                semanas: dict) -> list[str]:
    issues = []
    for t in ("ia","saude","resolve","logistica"):
        tt = sum(tickets[t])
        st = sum(status[t].values())
        ct = sum(channels[t].values())
        sw = sum(
            semanas[t][i]
            for i, mi in enumerate(semanas["mesIdx"])
            if mi in range(N_MONTHS)
        )
        if tt != st:
            issues.append(f"STATUS  {t}: tickets={tt} status={st} diff={st-tt:+d}")
        if tt != ct:
            issues.append(f"CHANNEL {t}: tickets={tt} channels={ct} diff={ct-tt:+d}")
        if tt != sw:
            issues.append(f"SEMANAS {t}: tickets={tt} semanas={sw} diff={sw-tt:+d}")
    return issues

# ── Tempos de atendimento (TMA/TMR/TMP) ──────────────────────────────────────

def build_tempos(df_t: pd.DataFrame) -> dict:
    """Computa TMA/TMR/TMP semanais em horas úteis (seg-sex) para Resolve e Saúde."""
    col_dt = "criado_em_brt" if "criado_em_brt" in df_t.columns else "criado_em"
    df = df_t.copy()
    df["_dt"] = pd.to_datetime(df[col_dt], utc=True, errors="coerce")
    if str(df["_dt"].dtype) != "datetime64[ns, America/Sao_Paulo]":
        df["_dt"] = df["_dt"].dt.tz_convert(BRT)

    # Apenas dias úteis (0=seg … 4=sex)
    df = df[df["_dt"].dt.dayofweek < 5]

    df["_wk"]   = df["_dt"].apply(lambda d: d.isocalendar().week if pd.notna(d) else None)
    df["_team"] = df.apply(_team, axis=1)

    all_weeks = sorted(w for w in df["_wk"].dropna().unique() if int(w) in WEEK_TO_MONTH)

    def weekly_avg(team_key: str, col: str) -> list:
        vals = []
        for w in all_weeks:
            sub = df[(df["_wk"] == w) & (df["_team"] == team_key)]
            if col in sub.columns:
                v = pd.to_numeric(sub[col], errors="coerce").dropna()
                vals.append(round(float(v.median()) / 60, 1) if len(v) > 0 else None)
            else:
                vals.append(None)
        return vals

    tma_col = next((c for c in ("resolucao_biz_min","resolucao_min") if c in df.columns), None)
    tmr_col = next((c for c in ("primeira_resposta_biz_min","primeira_resposta_min") if c in df.columns), None)
    tmp_col = next((c for c in ("pendencia_biz_min","pendencia_min") if c in df.columns), None)

    if not tma_col:
        log.warning("Colunas de métricas não encontradas — tempos não serão atualizados.")
        return {}

    return {
        "tma": {
            "resolve": weekly_avg("resolve", tma_col),
            "saude":   weekly_avg("saude",   tma_col),
        },
        "tmr": {
            "resolve": weekly_avg("resolve", tmr_col) if tmr_col else [],
            "saude":   weekly_avg("saude",   tmr_col) if tmr_col else [],
        },
        "tmp": {
            "resolve": weekly_avg("resolve", tmp_col) if tmp_col else [],
            "saude":   weekly_avg("saude",   tmp_col) if tmp_col else [],
        },
    }


def build_resolucoes_dia(df_t: pd.DataFrame) -> dict:
    """Conta tickets resolvidos/fechados por dia para cada time (resolve, saude, ia)."""
    col_dt = "atualizado_em"
    if col_dt not in df_t.columns:
        return {}

    df = df_t[df_t["status"].isin(["solved", "closed"])].copy()
    df["_dt"] = pd.to_datetime(df[col_dt], utc=True, errors="coerce")
    df = df.dropna(subset=["_dt"])
    df["_dt"] = df["_dt"].dt.tz_convert(BRT)
    df["_dia"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df["_team"] = df.apply(_team, axis=1)

    result = {}
    for team in ("resolve", "saude", "ia", "logistica"):
        sub = df[df["_team"] == team].groupby("_dia").size()
        result[team] = {k: int(v) for k, v in sub.items()}

    return result


def build_criados_dia(df_t: pd.DataFrame) -> dict:
    """Conta tickets criados por dia para cada time (resolve, saude, ia)."""
    col_dt = "criado_em_brt" if "criado_em_brt" in df_t.columns else "criado_em"
    df = df_t.copy()
    df["_dt"] = pd.to_datetime(df[col_dt], utc=True, errors="coerce")
    df = df.dropna(subset=["_dt"])
    df["_dt"] = df["_dt"].dt.tz_convert(BRT)
    df["_dia"] = df["_dt"].dt.strftime("%Y-%m-%d")
    df["_team"] = df.apply(_team, axis=1)

    result = {}
    for team in ("resolve", "saude", "ia", "logistica"):
        sub = df[df["_team"] == team].groupby("_dia").size()
        result[team] = {k: int(v) for k, v in sub.items()}

    return result

# ── Serialização JS ───────────────────────────────────────────────────────────

def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",",":"))

# ── Score: Pronto para Comprar ────────────────────────────────────────────────

_SC_PESOS_TAG = {
    "duvida_uso": 75, "duvida_produto": 75, "elogio": 70,
    "cupom": 65, "assinatura": 80, "documentacao": 55,
    "rastreio": 45, "entrega": 45, "financeiro": 40,
    "reclamacao_atendimento": 30, "reclamacao_produto": 20,
    "cancelamento": 10, "optout": 5,
}
_SC_W_CSAT, _SC_W_TIPO, _SC_W_TEMPO = 0.4, 0.3, 0.3

def _sc_peso_tipo(tags_str: str) -> float:
    for tag in str(tags_str or "").split("|"):
        if tag.strip() in _SC_PESOS_TAG:
            return float(_SC_PESOS_TAG[tag.strip()])
    return 50.0

def _sc_tempo_norm(minutos) -> float:
    try:
        m = float(minutos)
        if m != m:  # NaN check (NaN != NaN é sempre True)
            return 50.0
    except (TypeError, ValueError):
        return 50.0
    if m <= 10:  return 100.0
    if m >= 240: return 0.0
    return round(100 * (1 - (m - 10) / 230), 1)

def build_score_pronto_comprar(df_build: pd.DataFrame, df_csat: pd.DataFrame) -> dict:
    """Score de 'pronto para comprar' por cliente (requester_id), combinando
    CSAT (40%), tipo de ticket por tag (30%) e tempo de 1ª resposta (30%)."""
    if df_build.empty or df_csat.empty:
        return {}
    if "requester_id" not in df_build.columns:
        return {}

    csat_gb = df_csat[df_csat["score_raw"].isin(["good", "bad"])].copy()
    if csat_gb.empty:
        return {}

    csat_gb["ticket_id"] = csat_gb["ticket_id"].astype(str)
    t_cols = [c for c in ["ticket_id", "requester_id", "tags", "primeira_resposta_min"] if c in df_build.columns]
    df_t = df_build[t_cols].copy()
    df_t["ticket_id"] = df_t["ticket_id"].astype(str)

    merged = csat_gb.merge(df_t, on="ticket_id", how="inner")
    if merged.empty:
        return {}

    from collections import defaultdict as _dd
    por_req: dict = _dd(list)
    for _, row in merged.iterrows():
        s_csat = 100.0 if row["score_raw"] == "good" else 0.0
        s_tipo = _sc_peso_tipo(row.get("tags", ""))
        s_temp = _sc_tempo_norm(row.get("primeira_resposta_min"))
        score  = round(_SC_W_CSAT * s_csat + _SC_W_TIPO * s_tipo + _SC_W_TEMPO * s_temp, 1)
        try:
            rid = int(row["requester_id"])
        except (TypeError, ValueError):
            continue
        por_req[rid].append(score)

    ranking = sorted(
        [{"rid": rid, "s": round(sum(sc)/len(sc), 1), "n": len(sc)}
         for rid, sc in por_req.items()
         if sc and all(x == x for x in sc)],  # filtra listas com NaN
        key=lambda r: -r["s"]
    )
    total = len(ranking)
    hot  = sum(1 for r in ranking if r["s"] >= 70)
    warm = sum(1 for r in ranking if 40 <= r["s"] < 70)
    cold = sum(1 for r in ranking if r["s"] < 40)
    valid_scores = [r["s"] for r in ranking if r["s"] == r["s"]]
    med  = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else 0

    log.info(f"score_pronto_comprar: {total} clientes | hot={hot} warm={warm} cold={cold} | med={med}")
    return {"ranking": ranking[:100], "total": total, "hot": hot, "warm": warm, "cold": cold, "med": med}


def build_data_js(tickets, status, status_monthly, channels, channels_monthly, semanas, csat, tempos,
                  motivos_data, sub_data, perfis_data, n2_data, resolucoes_dia=None, criados_dia=None,
                  score_comprar=None) -> str:
    now = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
    tempos_js  = f"  tempos:{_js(tempos)},"  if tempos      else "  // tempos: nao disponivel"
    perfis_js  = f"  perfis:{_js(perfis_data)}," if perfis_data else "  perfis:{},"
    motivos_js = f"  motivos:{_js(motivos_data)}," if motivos_data else "  motivos:[],"
    sub_js     = f"  sub:{_js(sub_data)},"    if sub_data    else "  sub:{},"
    resol_js   = f"  resolucoes_dia:{_js(resolucoes_dia)}," if resolucoes_dia else "  resolucoes_dia:{},"
    criados_js = f"  criados_dia:{_js(criados_dia)}," if criados_dia else "  criados_dia:{},"
    sc_js      = f"  score_comprar:{_js(score_comprar)}," if score_comprar else "  score_comprar:{},"
    return f"""  // Gerado por updater.py em {now} — fonte: Zendesk API
  // tickets: {{{', '.join(f'{t}={sum(v)}' for t,v in tickets.items())}}}
  tickets:{_js(tickets)},
  status:{_js(status)},
  status_monthly:{_js(status_monthly)},
  channels:{_js(channels)},
  channels_monthly:{_js(channels_monthly)},
  semanas:{_js(semanas)},
  csat:{_js(csat)},
  n2:{_js(n2_data)},
{tempos_js}
{perfis_js}
{motivos_js}
{sub_js}
{resol_js}
{criados_js}
{sc_js}"""

# ── Injeção no HTML ───────────────────────────────────────────────────────────

# Substitui o bloco inteiro const D = { ... // END_DATA\n};
_RE_DATA = re.compile(
    r"(const D = \{).*?(// END_DATA\r?\n\r?\};)",
    re.DOTALL | re.MULTILINE,
)

def inject_into_html(html: str, data_js: str) -> str:
    match = _RE_DATA.search(html)
    if not match:
        raise RuntimeError(
            "Marcadores 'const D = {' ou '// END_DATA' nao encontrados no HTML."
        )
    new_html = (html[:match.start(1)]
                + "const D = {\n" + data_js
                + "\n// END_DATA\n};"
                + html[match.end(2):])
    return new_html

def update_sync_ts(html: str, ts: str) -> str:
    return re.sub(
        r'syncSetOK\(lastSync\|\|"[^"]*"\)',
        f'syncSetOK(lastSync||"{ts}")',
        html,
    )


def sync_tma_semanal():
    """Upserta TMA semanal por agente na tabela cx_tma_semanal (Supabase).
    Usa métricas de horas úteis (resolucao_biz_min, primeira_resposta_biz_min)
    e filtra apenas tickets criados em dias úteis (Seg–Sex).
    """
    try:
        import db_loader_metabase as _dbl
        from collections import defaultdict
        from datetime import datetime as _dt_cls

        sb      = _dbl.get_client()
        GRUPOS  = {42056691282323: "resolve", 43771604769299: "saude"}
        SKIP    = {"", "None", "Admin", "Logística Agentes", "Roberto venzi pires"}

        def _is_weekday(date_str):
            """Retorna True se a data cair em Seg–Sex."""
            try:
                s = str(date_str or "").strip().replace("Z", "+00:00").replace(" ", "T")
                return _dt_cls.fromisoformat(s).weekday() < 5
            except Exception:
                return True  # inclui se não conseguir parsear

        all_tickets = []
        for gid, grupo in GRUPOS.items():
            offset, batch = 0, 1000
            while True:
                rows = (
                    sb.table("tickets")
                      .select("assignee_id,nome_agente,semana_iso,"
                              "criado_em_brt,resolucao_biz_min,primeira_resposta_biz_min")
                      .eq("group_id", gid)
                      .range(offset, offset + batch - 1)
                      .execute()
                      .data
                ) or []
                for r in rows:
                    r["_grupo"] = grupo
                all_tickets.extend(rows)
                if len(rows) < batch:
                    break
                offset += batch

        agg = defaultdict(lambda: {
            "tma_sum": 0.0, "tma_n": 0,
            "tmr_sum": 0.0, "tmr_n": 0,
            "nome": "", "grupo": "",
        })
        for t in all_tickets:
            # Apenas dias úteis (Seg–Sex)
            if not _is_weekday(t.get("criado_em_brt")):
                continue
            nome = str(t.get("nome_agente") or "").strip()
            if nome in SKIP:
                continue
            aid = str(t.get("assignee_id") or "").split(".")[0]
            sem = str(t.get("semana_iso") or "").strip()
            if not aid or not sem:
                continue
            key = (aid, sem)
            agg[key]["nome"]  = nome
            agg[key]["grupo"] = t.get("_grupo", "")
            try:
                # TMA: resolucao_biz_min → horas úteis
                rbm = float(t.get("resolucao_biz_min") or 0)
                if 0 < rbm < 43200:  # < 30 dias em minutos
                    agg[key]["tma_sum"] += rbm / 60
                    agg[key]["tma_n"]   += 1
            except (ValueError, TypeError):
                pass
            try:
                # TMR: primeira_resposta_biz_min → horas úteis
                prbm = float(t.get("primeira_resposta_biz_min") or 0)
                if 0 < prbm < 2880:  # < 48h em minutos
                    agg[key]["tmr_sum"] += prbm / 60
                    agg[key]["tmr_n"]   += 1
            except (ValueError, TypeError):
                pass

        all_weeks = sorted({k[1] for k in agg})
        recent_8  = set(all_weeks[-8:])
        now_ts = datetime.now(BRT).isoformat()
        records = []
        for (aid, sem), d in sorted(agg.items()):
            if sem not in recent_8:
                continue
            if not d["tma_n"] and not d["tmr_n"]:
                continue
            try:
                aid_int = int(aid)
            except ValueError:
                continue
            records.append({
                "agente_id":    aid_int,
                "semana":       sem,
                "nome":         d["nome"],
                "grupo":        d["grupo"],
                "tma_h":        round(d["tma_sum"] / d["tma_n"], 1) if d["tma_n"] else None,
                "tmr_h":        round(d["tmr_sum"] / d["tmr_n"], 2) if d["tmr_n"] else None,
                "n_tickets":    d["tma_n"] or d["tmr_n"],
                "atualizado_em": now_ts,
            })

        if not records:
            log.warning("sync_tma_semanal: nenhum registro gerado")
            return

        sb.table("cx_tma_semanal").upsert(records, on_conflict="agente_id,semana").execute()
        log.info(f"cx_tma_semanal: {len(records)} registros upsertados ({len(recent_8)} semanas)")
    except Exception as e:
        log.warning(f"sync_tma_semanal falhou: {e}")


# ── Comentários e Offenders CSAT ─────────────────────────────────────────────

_RE_COMMENTS = re.compile(
    r"// ── CSAT COMMENTS.*?\nconst COMMENTS = \{.*?\};",
    re.DOTALL,
)
_RE_OFFENDERS = re.compile(
    r"// ── OFFENDERS by month.*?\nconst OFFENDERS = \[.*?\];",
    re.DOTALL,
)

THEMES_KW = [
    ("Atendimento por IA / Bot",   ["robô","ia","bot","automático","automática","robotizado","máquina","programado","genérica","prontas","chatbot"]),
    ("Demora no Atendimento",      ["demora","demorou","demorado","aguardar","lento","espera","esperei","horas","dias"]),
    ("Logística / Entrega",        ["rastreio","entrega","prazo","atrasado","atraso","correios","enviado","status","não recebi","não chegou"]),
    ("Sem Resposta",               ["sem resposta","não respondeu","não responderam","não atendeu","fantasma","ignorado","ninguém"]),
    ("Problema Não Resolvido",     ["não resolveu","não resolveram","nada foi","não foi resolvido","sem solução","sem resolução"]),
    ("Cancelamento / Pagamento",   ["cancelar","cancelamento","estorno","reembolso","dinheiro de volta","pagamento","cobrança"]),
    ("Receita / Documentação",     ["receita","documento","comprovante","anvisa","prescrição","vencida"]),
]

def build_comments_offenders(df_c: pd.DataFrame) -> tuple[dict, list]:
    import random as _rnd
    _rnd.seed(42)

    def _team(t):
        t = str(t or "")
        if "Saúde" in t or "Saude" in t: return "saude"
        if "Resolve" in t: return "resolve"
        return "ia"

    def _clean(txt):
        txt = str(txt or "").strip()
        if not txt or txt.lower() in ("nan","none",""): return ""
        txt = re.sub(r'\S+@\S+', '[email]', txt)
        txt = re.sub(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b', '[CPF]', txt)
        return " ".join(txt.split())

    MONTH_IDX = {"2026-01":0,"2026-02":1,"2026-03":2,"2026-04":3,"2026-05":4,"2026-06":5,"2026-07":6,"2026-08":7}
    has_com = df_c["comentario"].fillna("").str.strip() != ""
    scored  = df_c[df_c["score_raw"].isin(["good","bad"]) & has_com].copy()
    scored["m"]    = scored["ano_mes"].map(MONTH_IDX)
    scored = scored.dropna(subset=["m"]); scored["m"] = scored["m"].astype(int)
    scored["team"] = scored["time"].apply(_team)
    scored["txt"]  = scored["comentario"].apply(_clean)
    scored = scored[scored["txt"].str.len() >= 10]

    bad_all=[]; good_all=[]
    for m in range(N_MONTHS):
        sub = scored[scored["m"] == m]
        for r in sub[sub["score_raw"]=="bad"].to_dict("records"):
            bad_all.append({"m":m,"team":r["team"],"t":r["txt"],"id":str(int(r["ticket_id"])) if r.get("ticket_id") and str(r["ticket_id"]) not in ("nan","") else ""})
        for r in sub[sub["score_raw"]=="good"].to_dict("records"):
            good_all.append({"m":m,"team":r["team"],"t":r["txt"],"id":str(int(r["ticket_id"])) if r.get("ticket_id") and str(r["ticket_id"]) not in ("nan","") else ""})

    def _themes(texts):
        from collections import Counter
        counts=Counter(); examples={}
        for txt in texts:
            tl = txt.lower()
            for theme, kws in THEMES_KW:
                if any(k in tl for k in kws):
                    counts[theme] += 1
                    if theme not in examples: examples[theme] = txt[:140]
        result = []
        for theme, cnt in counts.most_common(4):
            pct = f"{cnt} menções" if len(texts)<50 else f"~{round(cnt/len(texts)*100)}%"
            result.append({"theme":theme,"ex":examples.get(theme,""),"cnt":pct})
        return result or [{"theme":"Sem dados suficientes","ex":"","cnt":"0"}]

    import pandas as _pd
    bad_df = _pd.DataFrame(bad_all)
    offenders=[]
    for m in range(N_MONTHS):
        grp = bad_df[bad_df["m"]==m]["t"].tolist() if len(bad_df)>0 else []
        offenders.append(_themes(grp))
    offenders.append(_themes(bad_df["t"].tolist() if len(bad_df)>0 else []))

    return {"bad":bad_all,"good":good_all}, offenders

def inject_comments(html: str, comments: dict, offenders: list) -> str:
    js = json.dumps

    comments_js = (
        "// ── CSAT COMMENTS — gerado por updater.py ─────────────────────────\n"
        f"const COMMENTS = {{\n  bad:{js(comments['bad'],ensure_ascii=False,separators=(',',':'))},\n"
        f"  good:{js(comments['good'],ensure_ascii=False,separators=(',',':'))}\n}};"
    )
    offenders_js = (
        "// ── OFFENDERS by month — gerado por updater.py ────────────────────\n"
        f"const OFFENDERS = {js(offenders,ensure_ascii=False,separators=(',',':'))};"
    )

    html2, n_com = _RE_COMMENTS.subn(comments_js, html)
    if n_com == 0:
        log.warning("Padrão COMMENTS não encontrado — bloco não substituído")
    html3, n_off = _RE_OFFENDERS.subn(offenders_js, html2)
    if n_off == 0:
        log.warning("Padrão OFFENDERS não encontrado — bloco não substituído")
    return html3

# ── Carrega dataset completo do Supabase para gerar o dashboard ───────────────

def _load_full_from_supabase() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Carrega todos os tickets e CSAT de 2026 do Supabase (paginado) para
    construir os blocos do dashboard.html com dados históricos completos."""
    import db_loader_zendesk as zdl
    sb = zdl.get_client()

    months = list(MONTH_IDX.keys())  # ex: ["2026-01", ..., "2026-07"]

    TICKET_COLS = (
        "ticket_id,criado_em,criado_em_brt,atualizado_em,status,canal,"
        "group_id,nome_grupo,time,assignee_id,atendido_por_ia,transferido_n2,"
        "resolvido_fcr,ano_mes,motivo,motivo_tag,submotivo_tag,submotivo_field_id,"
        "perfil,nome_agente,primeira_resposta_min,primeira_resposta_biz_min,"
        "resolucao_min,resolucao_biz_min,pendencia_min,pendencia_biz_min,"
        "requester_id,tags,assunto"
    )
    CSAT_COLS = (
        "csat_id,ticket_id,assignee_id,score_raw,score,comentario,"
        "avaliado_em,ano_mes,semana_iso,group_id,nome_grupo,time"
    )

    PAGE = 1000

    def fetch_all(table, cols, filters):
        rows, offset = [], 0
        while True:
            resp = (sb.table(table)
                      .select(cols)
                      .in_("ano_mes", filters)
                      .range(offset, offset + PAGE - 1)
                      .execute())
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < PAGE:
                break
            offset += PAGE
        return rows

    try:
        log.info("Supabase: carregando tickets históricos 2026...")
        t_rows = fetch_all("tickets", TICKET_COLS, months)
        log.info(f"  {len(t_rows)} tickets carregados")

        log.info("Supabase: carregando CSAT histórico 2026...")
        c_rows = fetch_all("csat", CSAT_COLS, months)
        log.info(f"  {len(c_rows)} registros CSAT carregados")

        df_t = pd.DataFrame(t_rows) if t_rows else pd.DataFrame()
        df_c = pd.DataFrame(c_rows) if c_rows else pd.DataFrame()
        return df_t, df_c
    except Exception as e:
        log.warning(f"Falha ao carregar histórico Supabase: {e}")
        return pd.DataFrame(), pd.DataFrame()


# ── Pipeline principal ────────────────────────────────────────────────────────

def collect_and_build(save_csv: bool = True) -> tuple[bool, list]:
    """Passo 1: Extração Zendesk → processamento → HTML → Supabase tickets/csat."""
    import extractor_2026 as _ext
    from config import START_DATE, END_DATE, CAMPO_MOTIVO_PAI, CAMPO_PERFIL, CAMPOS_SUBMOTIVO

    log.info("=== collect_and_build: início ===")

    # 1. Backup do dashboard
    if DASHBOARD.exists():
        shutil.copy2(DASHBOARD, BACKUP)
        log.info("Backup do dashboard criado.")

    # 2. Extração via extractor_2026 (sem subprocess)
    try:
        log.info("Buscando grupos...")
        groups = _ext.fetch_groups()
        log.info(f"  grupos: {len(groups)}")

        log.info("Buscando agentes...")
        agents = _ext.fetch_agents()
        log.info(f"  agentes: {len(agents)}")

        # Domingo (weekday==6): força sync completo para garantir consistência semanal
        is_sunday = datetime.now(BRT).weekday() == 6
        if is_sunday:
            log.info("Sync completo (domingo): cursor ignorado para reprocessar período inteiro")

        # Lê cursor incremental: Supabase primeiro, arquivo local como fallback
        since_ts = None
        if not is_sunday:
            cursor_str = _sb_state_get("zendesk_cursor")
            if cursor_str:
                try:
                    since_ts = int(cursor_str)
                    log.info(f"Zendesk incremental (Supabase) desde ts={since_ts} ({datetime.fromtimestamp(since_ts, tz=BRT).strftime('%d/%m/%Y %H:%M BRT')})")
                except ValueError:
                    since_ts = None
            if since_ts is None and ZD_SYNC_FILE.exists():
                try:
                    since_ts = int(ZD_SYNC_FILE.read_text().strip())
                    log.info(f"Zendesk incremental (arquivo) desde ts={since_ts} ({datetime.fromtimestamp(since_ts, tz=BRT).strftime('%d/%m/%Y %H:%M BRT')})")
                except (ValueError, OSError):
                    since_ts = None

        log.info("Buscando tickets + métricas...")
        tickets_raw, metrics_map, zd_end_time = _ext.fetch_tickets_with_metrics(since_ts=since_ts)
        log.info(f"  tickets_raw: {len(tickets_raw)}")

        log.info("Buscando CSAT...")
        ratings = _ext.fetch_csat(START_DATE, END_DATE)
        log.info(f"  ratings: {len(ratings)}")

        log.info("Buscando opções de campos customizados...")
        all_field_ids = [CAMPO_MOTIVO_PAI, CAMPO_PERFIL] + list(CAMPOS_SUBMOTIVO)
        opcoes = _ext.fetch_field_options(all_field_ids)
        opcoes_path = OUT / "campo_opcoes.json"
        opcoes_path.write_text(json.dumps(opcoes, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"  campo_opcoes.json salvo ({len(opcoes)} campos)")

        log.info("Construindo DataFrames...")
        df_tickets  = _ext.build_tickets(tickets_raw, groups)
        df_metrics  = _ext.build_metrics(metrics_map)
        df_agents   = _ext.build_agents(agents)
        df_groups   = _ext.build_groups(groups)

        # Enrich tickets com nome do agente
        df_tickets["assignee_id"] = pd.to_numeric(df_tickets["assignee_id"], errors="coerce").astype("Int64")
        df_agents_merge = df_agents[["agent_id", "nome"]].rename(columns={"agent_id": "assignee_id", "nome": "nome_agente"})
        df_agents_merge["assignee_id"] = df_agents_merge["assignee_id"].astype("Int64")
        df_tickets = df_tickets.merge(df_agents_merge, on="assignee_id", how="left")

        df_csat = _ext.build_csat(ratings, df_tickets)

        # Deduplica CSAT: quando cliente muda avaliação surgem múltiplos IDs para
        # o mesmo ticket. O Zendesk conta apenas o mais recente por ticket.
        if "avaliado_em" in df_csat.columns:
            df_csat["avaliado_em"] = pd.to_datetime(df_csat["avaliado_em"], utc=True, errors="coerce")
            before = len(df_csat)
            df_csat = (df_csat.sort_values("avaliado_em", ascending=False)
                               .drop_duplicates("ticket_id", keep="first")
                               .reset_index(drop=True))
            removed = before - len(df_csat)
            if removed:
                log.info(f"CSAT dedup: {removed} avaliações duplicadas removidas ({before} → {len(df_csat)})")

        # Merge métricas
        df_full = df_tickets.merge(df_metrics, on="ticket_id", how="left")

        log.info(f"  df_full: {len(df_full)} tickets  df_csat: {len(df_csat)}")

    except Exception as e:
        log.error(f"Extração Zendesk falhou: {e}")
        return False, [str(e)]

    # 3. Filtro CRM (tickets Blis Saúde sem assignee real)
    mask_crm = (df_full["time"] == "Blis Saúde") & (
        df_full["assignee_id"].isna() |
        (df_full["assignee_id"].astype(str).str.strip().isin(["", "nan"]))
    )
    n_crm = mask_crm.sum()
    if n_crm:
        df_full = df_full[~mask_crm].copy()
        log.info(f"Filtro CRM Blis Saude: {n_crm} sem assignee removidos -> {(df_full['time']=='Blis Saude').sum()} restantes")

    # 4. Validação
    issues = validate_csv(df_full)
    for i in issues:
        log.warning(i)

    # 5. Construção dos blocos — merge Supabase (histórico) + API (tickets novos)
    # Isso garante que tickets criados nesta hora aparecem no dashboard imediatamente,
    # sem aguardar a próxima execução.
    df_sb, df_csat_sb = _load_full_from_supabase()
    if not df_sb.empty and not df_full.empty and "ticket_id" in df_sb.columns and "ticket_id" in df_full.columns:
        df_build = (pd.concat([df_sb, df_full])
                      .drop_duplicates("ticket_id", keep="last")
                      .reset_index(drop=True))
        log.info(f"df_build: merge Supabase({len(df_sb)}) + API({len(df_full)}) = {len(df_build)} tickets")
    elif not df_sb.empty:
        df_build = df_sb
    else:
        df_build = df_full
    # Deduplica CSAT do Supabase: cliente pode ter mudado avaliação (bad→good),
    # gerando múltiplos csat_ids para o mesmo ticket. Manter só o mais recente
    # é o mesmo critério que o Zendesk usa. Sem isso, bads extras inflam a contagem.
    if not df_csat_sb.empty and "avaliado_em" in df_csat_sb.columns:
        df_csat_sb["avaliado_em"] = pd.to_datetime(df_csat_sb["avaliado_em"], utc=True, errors="coerce")
        df_csat_sb = (df_csat_sb.sort_values("avaliado_em", ascending=False)
                                 .drop_duplicates("ticket_id", keep="first")
                                 .reset_index(drop=True))
    # Sempre usa CSAT da API Zendesk: fetch_csat já é completo (START_DATE→END_DATE),
    # classifica por group_id da avaliação (critério nativo do Zendesk) e é deduplicado acima.
    # df_csat_sb tem mais linhas mas pode ter classificações desatualizadas.
    df_csat_build = df_csat
    log.info(f"Dataset para build: {len(df_build)} tickets, {len(df_csat_build)} CSAT (Zendesk API)")

    tickets_data      = build_tickets(df_build)
    status            = build_status(df_build)
    channels          = build_channels(df_build)
    channels_monthly  = build_channels_monthly(df_build)
    semanas           = build_semanas(df_build)
    csat              = build_csat(df_csat_build, df_build)
    tempos            = build_tempos(df_build)
    n2_data           = build_n2(df_build)
    status_monthly    = build_status_monthly(df_build)
    tag_names         = load_opcoes()
    motivos_data      = build_motivos_data(df_build)
    sub_data          = build_sub_data(df_build, tag_names)
    perfis_data       = build_perfis_data(df_build)
    resolucoes_dia    = build_resolucoes_dia(df_build)
    criados_dia       = build_criados_dia(df_build)
    score_comprar     = build_score_pronto_comprar(df_build, df_csat_build)

    log.info("Blocos construídos.")
    for t in ("ia","saude","resolve","logistica"):
        log.info(f"  {t}: tickets={sum(tickets_data[t])}  status={sum(status[t].values())}  channels={sum(channels[t].values())}")
    if tempos:
        log.info(f"  tempos: TMA Resolve={tempos['tma']['resolve']}  TMA Saude={tempos['tma']['saude']}")
    if motivos_data:
        log.info(f"  motivos: {len(motivos_data)} categorias  perfis: {len(perfis_data.get('labels',[]))}")

    # 6. Validação cruzada
    cross = cross_check(tickets_data, status, channels, semanas)
    for c in cross:
        log.warning(f"CROSS: {c}")
    issues.extend(cross)

    # 7. Injeção no HTML
    try:
        now_brt = datetime.now(BRT)
        ts = now_brt.strftime("%d/%m/%Y %H:%M")
        html = DASHBOARD.read_text(encoding="utf-8")
        data_js = build_data_js(tickets_data, status, status_monthly, channels, channels_monthly,
                                 semanas, csat, tempos, motivos_data, sub_data, perfis_data, n2_data,
                                 resolucoes_dia, criados_dia, score_comprar=score_comprar)
        html = inject_into_html(html, data_js)
        html = update_sync_ts(html, ts)

        # 8. Comentários e Offenders CSAT
        try:
            comments, offenders = build_comments_offenders(df_csat_build)
            html = inject_comments(html, comments, offenders)
            log.info(f"CSAT comments: {len(comments['bad'])} detratores, {len(comments['good'])} promotores injetados.")
        except Exception as ec:
            log.warning(f"Comentários CSAT nao injetados: {ec}")
            comments, offenders = {"bad": [], "good": []}, []

        DASHBOARD.write_text(html, encoding="utf-8")
        shutil.copy2(DASHBOARD, DASHBOARD_COPY)
        log.info("Dashboard atualizado com sucesso.")
    except Exception as e:
        log.error(f"Erro na injecao: {e}")
        if BACKUP.exists():
            shutil.copy2(BACKUP, DASHBOARD)
            log.info("Backup restaurado.")
        return False, issues + [str(e)]

    # 9. Escrever comments_data.json (db_loader_zendesk espera este arquivo)
    try:
        comments_path = OUT / "comments_data.json"
        comments_path.write_text(
            json.dumps({"bad": comments["bad"], "good": comments["good"], "offenders": offenders},
                       ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8"
        )
        log.info(f"comments_data.json salvo ({comments_path.stat().st_size:,} bytes)")
    except Exception as e:
        log.warning(f"comments_data.json nao salvo: {e}")

    # 10. Salvar CSVs (sempre True antes do sync Supabase)
    if save_csv:
        try:
            df_full.to_csv(OUT / "tabela_tickets.csv",  index=False, encoding="utf-8-sig")
            df_csat.to_csv(OUT / "tabela_csat.csv",     index=False, encoding="utf-8-sig")
            df_agents.to_csv(OUT / "tabela_agentes.csv", index=False, encoding="utf-8-sig")
            df_groups.to_csv(OUT / "tabela_grupos.csv",  index=False, encoding="utf-8-sig")
            df_metrics.to_csv(OUT / "tabela_metricas.csv", index=False, encoding="utf-8-sig")
            log.info(f"CSVs salvos: tickets={len(df_full)} csat={len(df_csat)} agentes={len(df_agents)} grupos={len(df_groups)}")
            _ext.write_audit_log(df_tickets, df_csat)
        except Exception as e:
            log.warning(f"Erro ao salvar CSVs: {e}")

    # 11. Sync Supabase Zendesk
    try:
        import db_loader_zendesk as zdl
        sb = zdl.get_client()
        log.info("Supabase Zendesk: iniciando sync...")
        try:
            zdl.load_tickets(sb)
        except Exception as e:
            log.warning(f"Supabase: load_tickets falhou: {e}")
        try:
            zdl.load_csat(sb)
        except Exception as e:
            log.warning(f"Supabase: load_csat falhou: {e}")
        try:
            zdl.load_agentes(sb)
        except Exception as e:
            log.warning(f"Supabase: load_agentes falhou: {e}")
        try:
            zdl.load_grupos(sb)
        except Exception as e:
            log.warning(f"Supabase: load_grupos falhou: {e}")
        try:
            zdl.load_comentarios_csat(sb)
        except Exception as e:
            log.warning(f"Supabase: load_comentarios_csat falhou: {e}")
        log.info("Supabase Zendesk: sync concluído.")
    except Exception as e:
        log.warning(f"Supabase Zendesk sync falhou (não crítico): {e}")

    # Salva cursor APÓS extração Zendesk — independente do status do Supabase.
    # O cursor representa "até onde a API foi lida", não "até onde o banco foi escrito".
    if zd_end_time:
        try:
            _sb_state_set("zendesk_cursor", str(zd_end_time))
            ZD_SYNC_FILE.write_text(str(zd_end_time))
            log.info(f"Cursor Zendesk salvo: {zd_end_time}")
        except Exception as e:
            log.warning(f"Cursor não salvo no Supabase (arquivo local OK): {e}")

    # 12. Sync volume diário (criados + resolvidos) → cx_volume_diario
    sync_volume_diario(criados_dia, resolucoes_dia)

    log.info("=== collect_and_build: concluído ===")
    return True, issues


def _sync_analytics_snapshot(result: dict) -> None:
    """Salva snapshot diário de KPIs analíticos na tabela cx_analytics_snapshot."""
    try:
        import db_loader_metabase as _dbl
        _sb = _dbl.get_client()

        safra_rows  = result.get("safraAnalise",     {}).get("data", {}).get("rows", [])
        tipo_rows   = result.get("tipoCliente",      {}).get("data", {}).get("rows", [])
        cli_rows    = result.get("clientesUnicos",   {}).get("data", {}).get("rows", [])
        bk_rows     = result.get("comportamentoKpis",{}).get("data", {}).get("rows", [])
        churn_rows  = result.get("churnMensal",      {}).get("data", {}).get("rows", [])

        total_clientes = cli_rows[0][0] if cli_rows else None
        total_safras   = len(safra_rows)

        # Taxa recompra: clientes não-Novo / total
        total_all = sum(r[1] for r in tipo_rows) if tipo_rows else 0
        total_rec = sum(r[1] for r in tipo_rows if r[0] != "Novo") if tipo_rows else 0
        taxa_recompra = round(total_rec / total_all * 100, 2) if total_all else None

        bk          = bk_rows[0] if bk_rows else None
        avg_dias_12 = bk[1] if bk else None
        inativos    = bk[3] if bk else None

        # Total churn (soma de todos os meses carregados)
        total_churn = sum(r[1] for r in churn_rows) if churn_rows else None

        now_brt = datetime.now(BRT)
        record  = {
            "data":               now_brt.date().isoformat(),
            "total_clientes":     int(total_clientes) if total_clientes is not None else None,
            "total_safras":       total_safras,
            "taxa_recompra_pct":  taxa_recompra,
            "avg_dias_1_2":       float(avg_dias_12) if avg_dias_12 is not None else None,
            "inativos":           int(inativos) if inativos is not None else None,
            "total_churn":        int(total_churn) if total_churn is not None else None,
            "atualizado_em":      now_brt.isoformat(),
        }
        _sb.table("cx_analytics_snapshot").upsert(record, on_conflict="data").execute()
        log.info(f"cx_analytics_snapshot: snapshot {record['data']} salvo — clientes={total_clientes} recompra={taxa_recompra}%")
    except Exception as e:
        log.warning(f"cx_analytics_snapshot falhou (não crítico): {e}")


def sync_metabase(save_js: bool = True) -> bool:
    """Passo 2: Metabase → Supabase (fallback JS gerado se save_js=True)."""
    log.info("=== sync_metabase: início ===")

    def mb_post(path, body=None, token=None):
        import time as _t
        url  = f"{METABASE_URL}{path}"
        data = json.dumps(body or {}).encode()
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["X-Metabase-Session"] = token
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read())
            except Exception as e:
                if attempt == 2:
                    raise
                wait = 10 * (attempt + 1)
                log.warning(f"Metabase {path} tentativa {attempt+1} falhou ({e}) — retry em {wait}s")
                _t.sleep(wait)

    def compact(resp, limit=None):
        d = resp.get("data", {})
        rows = d.get("rows", [])
        if limit:
            rows = rows[:limit]
        return {"data": {"cols": [{"name": c["name"]} for c in d.get("cols", [])], "rows": rows}}

    def mb_export_json(card_id, token):
        import urllib.parse
        hdrs = {"X-Metabase-Session": token, "Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode({"query": json.dumps({})}).encode()
        req  = urllib.request.Request(f"{METABASE_URL}/api/card/{card_id}/query/json", data=body, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    def aggregate_gasto(rows_export):
        from collections import defaultdict
        gasto = defaultdict(lambda: {"usos": 0, "receita": 0.0})
        for r in rows_export:
            code = r.get("code", "")
            fv   = r.get("full_value")
            if code and fv is not None:
                gasto[code]["usos"] += 1
                gasto[code]["receita"] += float(fv)
        cols = [{"name": "code"}, {"name": "receita_total"}, {"name": "usos"}, {"name": "ticket_medio"}]
        agg  = []
        for code, v in sorted(gasto.items(), key=lambda x: -x[1]["receita"]):
            ticket = round(v["receita"] / v["usos"], 2) if v["usos"] else 0
            agg.append([code, round(v["receita"], 2), v["usos"], ticket])
        return {"data": {"cols": cols, "rows": agg}}

    try:
        sess  = mb_post("/api/session", {"username": METABASE_EMAIL, "password": METABASE_PASS})
        token = sess["id"]
        result = {}

        # 2. Buscar todos os cards com try/except individual
        for key, card_id in MB_CARD_IDS.items():
            try:
                resp  = mb_post(f"/api/card/{card_id}/query", {"ignore_cache": True}, token=token)
                result[key] = compact(resp, MB_ROW_LIMITS.get(key))
                log.info(f"Metabase card {card_id} ({key}): OK ({len(result[key]['data']['rows'])} linhas)")
            except Exception as e:
                log.warning(f"Metabase card {card_id} ({key}): {e}")

        # 3. Cards especiais

        # couponsGasto — agregação completa do card 448
        try:
            rows_export = mb_export_json(448, token)
            result["couponsGasto"] = aggregate_gasto(rows_export)
            log.info(f"Metabase couponsGasto: OK ({len(rows_export)} pedidos, {len(result['couponsGasto']['data']['rows'])} cupons)")
        except Exception as e:
            log.warning(f"Metabase couponsGasto: {e}")

        # baseRecompra — card 404+203
        try:
            from collections import defaultdict
            from datetime import date as _date
            rows_export_404 = mb_export_json(404, token)
            today_d = _date.today()
            safra_map = defaultdict(lambda: {
                'total': 0, 'com_recompra': 0, 'total_pedidos': 0,
                'dias_1_2_sum': 0, 'dias_1_2_cnt': 0,
                'ativou_90d': 0, 'inativos': 0,
                'n_1': 0, 'n_2': 0, 'n_3': 0, 'n_4plus': 0,
                '_inativo_idades': [],  # idades (dias) de clientes com 1 pedido
            })
            tipo_map  = defaultdict(lambda: {'total': 0, 'total_pedidos': 0, 'receita': 0.0})
            TIPOS = [('Novo', 1, 1), ('Recorrente', 2, 3), ('Fiel', 4, 6), ('Alta Frequência', 7, 9999)]
            user_tipo = {}
            tipo_mes_map = defaultdict(lambda: defaultdict(lambda: {'total': 0, 'total_pedidos': 0, 'receita': 0.0}))
            user_mes = {}

            for r in rows_export_404:
                tot   = int(r.get('total_orders') or 1)
                first = str(r.get('first_order_date') or '')[:7]
                uid   = str(r.get('user_id') or '')
                tipo  = next((t for t,lo,hi in TIPOS if lo <= tot <= hi), 'Alta Frequência')
                if uid:
                    user_tipo[uid] = tipo

                if first and len(first) == 7:
                    sd = safra_map[first]
                    sd['total']        += 1
                    sd['com_recompra'] += (1 if tot > 1 else 0)
                    sd['total_pedidos'] += tot
                    if tot == 1:       sd['n_1']     += 1
                    elif tot == 2:     sd['n_2']     += 1
                    elif tot == 3:     sd['n_3']     += 1
                    else:              sd['n_4plus']  += 1

                    d1 = str(r.get('first_order_date') or '')[:10]
                    d2 = str(r.get('second_order_date') or '')[:10]
                    if d2 and d2 != 'None' and len(d2) == 10:
                        try:
                            diff = (_date.fromisoformat(d2) - _date.fromisoformat(d1)).days
                            if 0 < diff < 1000:
                                sd['dias_1_2_sum'] += diff
                                sd['dias_1_2_cnt'] += 1
                                if diff <= 90:
                                    sd['ativou_90d'] += 1
                        except Exception:
                            pass

                    if tot == 1 and len(d1) == 10:
                        try:
                            age = (today_d - _date.fromisoformat(d1)).days
                            sd['_inativo_idades'].append(age)
                        except Exception:
                            pass

                tipo_map[tipo]['total']        += 1
                tipo_map[tipo]['total_pedidos'] += tot
                if first and len(first) == 7:
                    tipo_mes_map[first][tipo]['total']         += 1
                    tipo_mes_map[first][tipo]['total_pedidos'] += tot
                if uid and first and len(first) == 7:
                    user_mes[uid] = first

            safra_cols = [{'name': c} for c in [
                'safra','total_usuarios','com_recompra','pct_recompra','media_pedidos',
                'avg_dias_1_2','inativos','pct_ativou_90d',
                'n_1','n_2','n_3','n_4plus',
            ]]
            # 2ª passagem: calcula inativos usando o intervalo médio da própria safra
            # como limite (em vez de 90 dias fixo). Isso corrige safras recentes
            # cujo intervalo médio é menor que 90d (ex: maio com 45d).
            for safra, d in safra_map.items():
                avg_d12_safra = round(d['dias_1_2_sum'] / d['dias_1_2_cnt'], 1) if d['dias_1_2_cnt'] else 90
                threshold = max(int(avg_d12_safra), 30)
                d['inativos'] = sum(1 for age in d['_inativo_idades'] if age > threshold)

            safra_rows = []
            for safra in sorted(safra_map.keys()):
                d = safra_map[safra]
                avg_d12 = round(d['dias_1_2_sum'] / d['dias_1_2_cnt'], 1) if d['dias_1_2_cnt'] else None
                pct90   = round(d['ativou_90d'] / d['com_recompra'], 4) if d['com_recompra'] else None
                safra_rows.append([
                    safra, d['total'], d['com_recompra'],
                    round(d['com_recompra'] / d['total'], 4) if d['total'] else 0,
                    round(d['total_pedidos'] / d['total'], 2) if d['total'] else 0,
                    avg_d12, d['inativos'], pct90,
                    d['n_1'], d['n_2'], d['n_3'], d['n_4plus'],
                ])
            result['safraAnalise'] = {'data': {'cols': safra_cols, 'rows': safra_rows}}

            # Receita por tipo — card 734 (user_id + receita_total por pedido pago)
            try:
                rows_734 = mb_export_json(734, token)
                for r734 in rows_734:
                    uid734   = str(r734.get('user_id') or '')
                    total734 = float(r734.get('receita_total') or 0)
                    if uid734 and total734 > 0:
                        t = user_tipo.get(uid734)
                        if t:
                            tipo_map[t]['receita'] += total734
                            mes_u = user_mes.get(uid734)
                            if mes_u:
                                tipo_mes_map[mes_u][t]['receita'] += total734
                log.info(f"Metabase card 734: {len(rows_734)} usuários processados para ticket_medio")
            except Exception as e:
                log.warning(f"Metabase card 734 (receita): {e}")

            total_clientes = sum(d['total'] for d in tipo_map.values()) or 1
            tipo_cols = [{'name': c} for c in ['tipo_cliente','qtd_usuarios','pct_do_total','media_pedidos','receita_total','ticket_medio']]
            tipo_rows = []
            for tipo, lo, hi in TIPOS:
                d = tipo_map.get(tipo, {'total': 0, 'total_pedidos': 0, 'receita': 0.0})
                if d['total']:
                    ticket = round(d['receita'] / d['total_pedidos'], 2) if d['total_pedidos'] else None
                    tipo_rows.append([
                        tipo, d['total'],
                        round(d['total'] / total_clientes, 4),
                        round(d['total_pedidos'] / d['total'], 2),
                        round(d['receita'], 2) if d['receita'] else None,
                        ticket,
                    ])
            result['tipoCliente'] = {'data': {'cols': tipo_cols, 'rows': tipo_rows}}

            # tipo_cliente mensal (por first_order_date mês)
            tipo_mes_cols = [{'name': c} for c in ['periodo','tipo_cliente','qtd_usuarios','pct_do_total','media_pedidos','receita_total','ticket_medio']]
            tipo_mes_rows_out = []
            for mes in sorted(tipo_mes_map.keys()):
                total_mes = sum(tipo_mes_map[mes][t]['total'] for t in tipo_mes_map[mes]) or 1
                for tipo, _, _ in TIPOS:
                    d = tipo_mes_map[mes].get(tipo, {'total': 0, 'total_pedidos': 0, 'receita': 0.0})
                    if not d['total']:
                        continue
                    t_ticket = round(d['receita'] / d['total_pedidos'], 2) if d['total_pedidos'] else None
                    tipo_mes_rows_out.append([
                        mes + '-01',
                        tipo,
                        d['total'],
                        round(d['total'] / total_mes, 4),
                        round(d['total_pedidos'] / d['total'], 2) if d['total'] else 0,
                        round(d['receita'], 2) if d['receita'] else None,
                        t_ticket,
                    ])
            result['tipoClienteMensal'] = {'data': {'cols': tipo_mes_cols, 'rows': tipo_mes_rows_out}}

            all_dias = [(safra_map[s]['dias_1_2_sum'], safra_map[s]['dias_1_2_cnt']) for s in safra_map]
            total_sum = sum(x[0] for x in all_dias)
            total_cnt = sum(x[1] for x in all_dias)
            global_avg_d12 = round(total_sum / total_cnt, 1) if total_cnt else None
            inativos_total  = sum(safra_map[s]['inativos'] for s in safra_map)
            ativou90_total  = sum(safra_map[s]['ativou_90d'] for s in safra_map)
            com_rec_total   = sum(safra_map[s]['com_recompra'] for s in safra_map)
            pct_ativou_90   = round(ativou90_total / com_rec_total, 4) if com_rec_total else None
            bk_cols = [{'name': c} for c in ['id','avg_dias_1_2','pct_ativou_90d','inativos_global']]
            result['comportamentoKpis'] = {'data': {'cols': bk_cols, 'rows': [['global', global_avg_d12, pct_ativou_90, inativos_total]]}}

            log.info(f"Metabase baseRecompra (404): {len(rows_export_404)} clientes → {len(safra_rows)} safras, {len(tipo_rows)} tipos | inativos={inativos_total} avg_d12={global_avg_d12}d")
        except Exception as e:
            log.warning(f"Metabase baseRecompra (404): {e}")

        # intervaloPedidos — cards 82+191
        try:
            r82  = mb_post(f"/api/card/82/query",  {"ignore_cache": True}, token=token)
            r191 = mb_post(f"/api/card/191/query", {"ignore_cache": True}, token=token)
            rows82  = r82.get('data',{}).get('rows',[])
            rows191 = r191.get('data',{}).get('rows',[])
            avg12 = rows82[0][1]  if rows82  else None
            avg23 = rows191[0][2] if rows191 else None
            ik_cols = [{'name': c} for c in ['avg_dias_1_2','avg_dias_2_3']]
            result['intervaloPedidos'] = {'data': {'cols': ik_cols, 'rows': [[avg12, avg23]]}}
            log.info(f"Metabase intervaloPedidos: 1→2={avg12:.1f}d  2→3={avg23:.1f}d" if avg12 else "Metabase intervaloPedidos: sem dados")
        except Exception as e:
            log.warning(f"Metabase intervaloPedidos (82/191): {e}")

        # churnMensal — card 209
        try:
            from collections import defaultdict as _dd
            rows_209 = mb_export_json(209, token)
            churn_map = _dd(int)
            for r in rows_209:
                mes = str(r.get('churn_mês') or r.get('churn_mes') or '')[:7]
                if mes and len(mes) == 7:
                    churn_map[mes] += 1
            churn_cols = [{'name': c} for c in ['churn_mes','qtd_churn']]
            churn_rows = [[m, churn_map[m]] for m in sorted(churn_map)]
            result['churnMensal'] = {'data': {'cols': churn_cols, 'rows': churn_rows}}
            log.info(f"Metabase churnMensal: {len(churn_rows)} meses ({len(rows_209)} clientes em churn)")
        except Exception as e:
            log.warning(f"Metabase churnMensal (209): {e}")

        # performanceMedicosMensal — card 143 via dashboard API filtrado por mês
        try:
            import calendar as _cal
            now_d = datetime.now(tz=timezone(timedelta(hours=-3)))
            mensal_rows = []
            for year in [2026]:
                last_m = now_d.month if year == now_d.year else 12
                for month in range(1, last_m + 1):
                    start = f"{year}-{month:02d}-01"
                    end   = f"{year}-{month:02d}-{_cal.monthrange(year, month)[1]:02d}"
                    periodo = f"{year}-{month:02d}-01"
                    body_m = {
                        "parameters": [
                            {"type": "date/single", "id": "5734d13",  "value": start, "target": ["variable", ["template-tag", "start_date"]]},
                            {"type": "date/single", "id": "ac4b8018", "value": end,   "target": ["variable", ["template-tag", "end_date"]]},
                        ]
                    }
                    resp_m = mb_post("/api/dashboard/1/dashcard/146/card/143/query", body_m, token=token)
                    rows_m = resp_m.get("data", {}).get("rows", [])
                    for r in rows_m:
                        mensal_rows.append([
                            periodo,          # 0 periodo
                            r[1],             # 1 nome_medico
                            r[0],             # 2 doctor_id
                            r[2],             # 3 status_doctor
                            r[3],             # 4 consultas_criadas
                            r[4],             # 5 quantidade_orders
                            r[7],             # 6 consultas_finalizadas
                            r[8],             # 7 consultas_canceladas
                            r[10],            # 8 quantidade_nfs
                            r[11],            # 9 R$ total consultas
                            r[12],            # 10 média_de_avaliações
                            r[13],            # 11 NPS
                            r[14],            # 12 avg_steps (tempo médio min)
                        ])
                    log.info(f"Metabase performanceMedicosMensal {periodo}: {len(rows_m)} médicos")
            cols_m = ["periodo","nome_medico","doctor_id","status_doctor","consultas_criadas",
                      "quantidade_orders","consultas_finalizadas","consultas_canceladas",
                      "quantidade_nfs","R$ total consultas","média_de_avaliações","NPS","avg_steps"]
            result["performanceMedicosMensal"] = {
                "data": {"cols": [{"name": c} for c in cols_m], "rows": mensal_rows}
            }
            log.info(f"Metabase performanceMedicosMensal: {len(mensal_rows)} linhas no total")
        except Exception as e:
            log.warning(f"Metabase performanceMedicosMensal: {e}")

        # protocoloMensal — card 445 só mês atual; meses passados vêm do Supabase (evita timeout)
        try:
            import calendar as _cal2
            now_d2   = datetime.now(tz=timezone(timedelta(hours=-3)))
            cur_year = now_d2.year
            cur_mon  = now_d2.month
            start_cur = f"{cur_year}-{cur_mon:02d}-01"
            end_cur   = f"{cur_year}-{cur_mon:02d}-{_cal2.monthrange(cur_year, cur_mon)[1]:02d}"
            periodo_cur = f"{cur_year}-{cur_mon:02d}-01"

            # Fetch current month with longer timeout
            body_p = {
                "ignore_cache": True,
                "parameters": [
                    {"type": "date/single", "id": "bbc23224-c185-4e1b-8037-d1ca62d80a69", "value": start_cur, "target": ["variable", ["template-tag", "data_inicio"]]},
                    {"type": "date/single", "id": "c35fed4a-a668-40b9-a420-fea28864647f", "value": end_cur,   "target": ["variable", ["template-tag", "data_fim"]]},
                ]
            }
            url_445 = f"{METABASE_URL}/api/card/445/query"
            data_p  = json.dumps(body_p).encode()
            hdrs_p  = {"Content-Type": "application/json", "X-Metabase-Session": token}
            req_p   = urllib.request.Request(url_445, data=data_p, headers=hdrs_p, method="POST")
            with urllib.request.urlopen(req_p, timeout=180) as r_p:
                resp_p = json.loads(r_p.read())
            rows_p = resp_p.get("data", {}).get("rows", [])
            cur_rows = [[periodo_cur, r[0], r[1], r[2], r[3]] for r in rows_p if r and r[0]]
            log.info(f"Metabase protocoloMensal {periodo_cur}: {len(rows_p)} protocolos ({sum(int(r[3]) for r in rows_p if r and len(r)>3)} pedidos)")

            # Load all months from Supabase; current month will be upserted later
            import db_loader_metabase as _dbl_pm
            _sb_pm  = _dbl_pm.get_client()
            sb_rows = _sb_pm.table("mb_protocolo_mensal").select("*").order("periodo").execute().data or []
            past_rows = [
                [str(r["periodo"])[:7] + "-01", r["protocolo"], r["soma_receita"], r["qtd_pedidos"], r["ticket_medio"]]
                for r in sb_rows if str(r.get("periodo",""))[:7] != f"{cur_year}-{cur_mon:02d}"
            ]
            proto_mensal_rows = past_rows + cur_rows
            proto_m_cols = [{"name": c} for c in ["periodo", "protocolo", "soma_receita", "qtd_pedidos", "ticket_medio"]]
            result["protocoloMensal"] = {"data": {"cols": proto_m_cols, "rows": proto_mensal_rows}}
            log.info(f"Metabase protocoloMensal: {len(proto_mensal_rows)} linhas ({len(past_rows)} Supabase + {len(cur_rows)} atuais)")
        except Exception as e:
            log.warning(f"Metabase protocoloMensal (445 mensal): {e}")

        # 4. Salvar metabase_data.js (fallback do browser)
        if save_js:
            ts_brt = datetime.now(tz=timezone(timedelta(hours=-3))).strftime("%Y-%m-%dT%H:%M:%S")
            js = f"/* Metabase preloaded — {ts_brt} BRT */\nwindow.MB_PRELOADED={json.dumps(result, ensure_ascii=False, separators=(',',':'))};\n"
            MB_DATA_JS.write_text(js, encoding="utf-8")
            log.info(f"Metabase: {MB_DATA_JS} salvo ({MB_DATA_JS.stat().st_size:,} bytes)")

        # 5. Sync Supabase (usa dict em memória — sem ler do disco)
        try:
            import db_loader_metabase as _dbl
            _sb = _dbl.get_client()
            mb = result  # dict em memória, sem I/O

            _has445 = bool(mb.get("pedidosProtocolo")) or bool(mb.get("protocoloMensal"))

            _dbl._run(_dbl.load_clientes_resumo,            _sb, mb)
            _dbl._run(_dbl.load_safra_analise,              _sb, mb)
            _dbl._run(_dbl.load_recompra_mensal,            _sb, mb)
            _dbl._run(_dbl.load_comportamento_kpis,         _sb, mb)
            _dbl._run(_dbl.load_churn_mensal,               _sb, mb)
            _dbl._run(_dbl.load_funil_canal,                _sb, mb)
            _dbl._run(_dbl.load_cohort_pedidos,             _sb, mb)
            _dbl._run(_dbl.load_cancelamentos,              _sb, mb)
            _dbl._run(_dbl.load_nps_periodo,                _sb, mb)
            _dbl._run(_dbl.load_totais_diarios,             _sb, mb)
            _dbl._run(_dbl.load_performance_medicos_mensal, _sb, mb)
            _dbl._run(_dbl.load_status_temporal,            _sb, mb)
            _dbl._run(_dbl.load_cancelamentos,              _sb, mb)
            _dbl._run(_dbl.load_protocolo_mensal,           _sb, mb)

            # tipo_cliente vem de cards 404+734 (independente do card 445)
            _dbl._run(_dbl.load_tipo_cliente,         _sb, mb)
            _dbl._run(_dbl.load_tipo_cliente_mensal, _sb, mb)

            # Proteção Card 445: só atualiza protocoloAnalise (acumulado) se card 445 respondeu
            if _has445:
                _dbl._run(_dbl.load_protocolo_analise, _sb, mb)
                log.info("Supabase: protocoloAnalise atualizado com card 445.")
            else:
                log.warning("Card 445 ausente — protocoloAnalise preservado do dia anterior.")

            log.info("Supabase Metabase: sync concluído.")
        except Exception as e:
            log.warning(f"Supabase Metabase sync falhou (não crítico): {e}")

        # Snapshot diário de KPIs analíticos → cx_analytics_snapshot
        _sync_analytics_snapshot(result)

        log.info("=== sync_metabase: concluído ===")
        return True

    except Exception as e:
        log.error(f"Metabase fetch falhou: {e}")
        return False


def sync_saude_recompra() -> bool:
    """Cohort recompra Blis Saúde: com vs sem contato Saúde → recompra_saude_cohort.
    Período: Jan 2025 – mês atual. Agrupamento: first_order_date (safra de compra).
    """
    log.info("=== sync_saude_recompra: início ===")
    try:
        import urllib.parse
        from collections import defaultdict
        from datetime import datetime as _dt

        def _mb_post(path, body=None, token=None):
            url  = f"{METABASE_URL}{path}"
            data = json.dumps(body or {}).encode()
            hdrs = {"Content-Type": "application/json"}
            if token:
                hdrs["X-Metabase-Session"] = token
            req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())

        sess  = _mb_post("/api/session", {"username": METABASE_EMAIL, "password": METABASE_PASS})
        token = sess["id"]
        log.info("sync_saude_recompra: Metabase auth OK")

        # Card 733: todos os usuários Blis Saúde com first_order_date, total_orders, zendesk_id
        hdrs733 = {"X-Metabase-Session": token, "Content-Type": "application/x-www-form-urlencoded"}
        body733 = urllib.parse.urlencode({"query": json.dumps({})}).encode()
        req733  = urllib.request.Request(
            f"{METABASE_URL}/api/card/733/query/json",
            data=body733, headers=hdrs733, method="POST"
        )
        with urllib.request.urlopen(req733, timeout=300) as r:
            rows_733 = json.loads(r.read())
        log.info(f"sync_saude_recompra: card 733 → {len(rows_733)} usuários")

        import db_loader_metabase as _dbl
        _sb = _dbl.get_client()

        # Set de zendesk_ids que abriram ticket em Blis Saúde (paginado)
        saude_contact_ids = set()
        batch, offset = 1000, 0
        while True:
            rows = (
                _sb.table("tickets")
                   .select("requester_id")
                   .eq("group_id", 43771604769299)
                   .range(offset, offset + batch - 1)
                   .execute()
                   .data
            ) or []
            for t in rows:
                rid = t.get("requester_id")
                if rid and str(rid) != "None":
                    saude_contact_ids.add(str(rid))
            if len(rows) < batch:
                break
            offset += batch
        log.info(f"sync_saude_recompra: {len(saude_contact_ids)} zendesk_ids únicos contataram Saúde")

        # Agrega por safra (first_order_date mês, formato YYYY-MM)
        # Período: Jan 2025 – mês atual
        now_brt    = _dt.now(tz=timezone(timedelta(hours=-3)))
        mes_limite = now_brt.strftime("%Y-%m")
        monthly = defaultdict(lambda: {
            "total": 0, "recomp": 0,
            "saude_total": 0, "saude_recomp": 0,
        })
        for r in rows_733:
            first = str(r.get("first_order_date") or "")[:7]
            if not first or len(first) != 7 or first < "2025-01" or first > mes_limite:
                continue
            orders  = int(r.get("total_orders") or 0)
            zid_raw = r.get("zendesk_id")
            try:
                zid = str(int(float(zid_raw))) if zid_raw else None
            except (ValueError, TypeError):
                zid = None

            monthly[first]["total"]  += 1
            if orders >= 2:
                monthly[first]["recomp"] += 1
            if zid and zid in saude_contact_ids:
                monthly[first]["saude_total"] += 1
                if orders >= 2:
                    monthly[first]["saude_recomp"] += 1

        # Monta records para upsert em recompra_saude_cohort
        now_ts  = _dt.now(tz=timezone(timedelta(hours=-3))).isoformat()
        records = []
        for mes in sorted(monthly.keys()):
            m              = monthly[mes]
            total          = m["total"]
            recomp         = m["recomp"]
            cl_saude       = m["saude_total"]
            rec_saude      = m["saude_recomp"]
            cl_sem         = total - cl_saude
            rec_sem        = recomp - rec_saude
            if total < 10:
                continue
            pct_total = round(recomp    / total    * 100, 2) if total    else None
            pct_saude = round(rec_saude / cl_saude * 100, 2) if cl_saude else None
            pct_sem   = round(rec_sem   / cl_sem   * 100, 2) if cl_sem   else None
            records.append({
                "safra":                 mes + "-01",   # DATE: primeiro dia do mês
                "total_clientes":        total,
                "total_recompra":        recomp,
                "clientes_saude":        cl_saude,
                "recompra_saude":        rec_saude,
                "clientes_sem_saude":    cl_sem,
                "recompra_sem_saude":    rec_sem,
                "pct_recompra_total":    pct_total,
                "pct_recompra_saude":    pct_saude,
                "pct_recompra_sem_saude": pct_sem,
                "atualizado_em":         now_ts,
            })

        n = _dbl.upsert_batch(_sb, "recompra_saude_cohort", records, "safra")
        log.info(f"recompra_saude_cohort → {n} safras upsertadas")
        log.info("=== sync_saude_recompra: concluído ===")
        return True

    except Exception as e:
        import traceback
        log.error(f"sync_saude_recompra falhou: {e}\n{traceback.format_exc()}")
        return False


def sync_volume_diario(criados_dia: dict, resolucoes_dia: dict) -> bool:
    """Upserta volumes diários (criados + resolvidos) por time em cx_volume_diario."""
    log.info("=== sync_volume_diario: início ===")
    try:
        import db_loader_metabase as _dbl
        sb = _dbl.get_client()
        now_ts = datetime.now(BRT).isoformat()

        # Une todas as datas de todos os times
        records = []
        all_teams = set(list(criados_dia.keys()) + list(resolucoes_dia.keys()))
        for team in all_teams:
            all_dates = set(list(criados_dia.get(team, {}).keys()) +
                            list(resolucoes_dia.get(team, {}).keys()))
            for data in all_dates:
                records.append({
                    "data":          data,
                    "time":          team,
                    "criados":       criados_dia.get(team, {}).get(data, 0),
                    "resolvidos":    resolucoes_dia.get(team, {}).get(data, 0),
                    "atualizado_em": now_ts,
                })

        n = _dbl.upsert_batch(sb, "cx_volume_diario", records, "data,time")
        log.info(f"cx_volume_diario → {n} registros upsertados")
        log.info("=== sync_volume_diario: concluído ===")
        return True
    except Exception as e:
        import traceback
        log.error(f"sync_volume_diario falhou: {e}\n{traceback.format_exc()}")
        return False


def sync_agent_performance() -> bool:
    """Agrega performance mensal por agente (Resolve + Saúde) → cx_performance_agentes."""
    log.info("=== sync_agent_performance: início ===")
    try:
        from collections import defaultdict
        from datetime import datetime as _dt
        import db_loader_metabase as _dbl

        sb = _dbl.get_client()

        GRUPOS = {
            "resolve": 42056691282323,   # Blis Resolve
            "saude":   43771604769299,   # Blis Saúde
            "ia":      50304023554451,   # Cloud Humans (IA)
        }
        SKIP_AGENTS = {"", "None", "Admin", "Logística Agentes", "Roberto venzi pires"}

        # Tabela de nomes por agent_id (fallback quando nome_agente está null no ticket)
        agents_rows = sb.table("agentes").select("agent_id,nome").execute().data or []
        agents_map = {str(a["agent_id"]): str(a.get("nome") or "").strip() for a in agents_rows}
        log.info(f"sync_agent_performance: {len(agents_map)} agentes carregados do lookup")

        # Lê tickets dos três grupos (inclui ano_mes para filtrar só 2026)
        months_filter = list(MONTH_IDX.keys())
        all_tickets = []
        for grupo_nome, grupo_id in GRUPOS.items():
            offset, batch = 0, 1000
            while True:
                rows = (
                    sb.table("tickets")
                      .select("assignee_id,nome_agente,ano_mes,status,resolucao_h")
                      .eq("group_id", grupo_id)
                      .in_("ano_mes", months_filter)
                      .range(offset, offset + batch - 1)
                      .execute()
                      .data
                ) or []
                for r in rows:
                    r["_grupo"] = grupo_nome
                all_tickets.extend(rows)
                if len(rows) < batch:
                    break
                offset += batch

        # Lê CSAT dos três grupos
        all_csat = []
        for grupo_id in GRUPOS.values():
            offset, batch = 0, 1000
            while True:
                rows = (
                    sb.table("csat")
                      .select("assignee_id,ano_mes,score_raw")
                      .eq("group_id", grupo_id)
                      .in_("score_raw", ["good", "bad"])
                      .in_("ano_mes", months_filter)
                      .range(offset, offset + batch - 1)
                      .execute()
                      .data
                ) or []
                all_csat.extend(rows)
                if len(rows) < batch:
                    break
                offset += batch

        log.info(f"sync_agent_performance: {len(all_tickets)} tickets, {len(all_csat)} avaliações CSAT")

        # Agrega tickets por (mes, assignee_id)
        t_agg = defaultdict(lambda: {"total": 0, "resolvidos": 0, "res_h": [], "nome": "", "grupo": ""})
        for t in all_tickets:
            aid = str(t.get("assignee_id") or "").split(".")[0]
            if not aid or aid == "None":
                continue
            # Prioriza nome_agente do ticket; fallback para tabela agentes
            nome = str(t.get("nome_agente") or "").strip()
            if not nome or nome in SKIP_AGENTS:
                nome = agents_map.get(aid, "").strip()
            if not nome or nome in SKIP_AGENTS:
                continue
            mes = str(t.get("ano_mes") or "")[:7]
            if len(mes) != 7:
                continue
            key = (mes, aid)
            t_agg[key]["total"]   += 1
            t_agg[key]["nome"]     = nome
            t_agg[key]["grupo"]    = t.get("_grupo", "")
            if str(t.get("status", "")).lower() in ("closed", "solved"):
                t_agg[key]["resolvidos"] += 1
            try:
                rh = float(t.get("resolucao_h") or 0)
                if 0 < rh < 720:
                    t_agg[key]["res_h"].append(rh)
            except (ValueError, TypeError):
                pass

        # Agrega CSAT por (mes, assignee_id)
        c_agg = defaultdict(lambda: {"good": 0, "bad": 0})
        for c in all_csat:
            aid = str(c.get("assignee_id") or "").split(".")[0]
            mes = str(c.get("ano_mes") or "")[:7]
            if not aid or len(mes) != 7:
                continue
            if c.get("score_raw") == "good":
                c_agg[(mes, aid)]["good"] += 1
            elif c.get("score_raw") == "bad":
                c_agg[(mes, aid)]["bad"]  += 1

        now_ts  = _dt.now(tz=timezone(timedelta(hours=-3))).isoformat()
        records = []
        for (mes, aid), d in sorted(t_agg.items()):
            if not d["total"]:
                continue
            good = c_agg[(mes, aid)]["good"]
            bad  = c_agg[(mes, aid)]["bad"]
            tma  = round(sum(d["res_h"]) / len(d["res_h"]), 1) if d["res_h"] else None
            csat_s = round(good / (good + bad) * 100, 1) if (good + bad) >= 5 else None
            try:
                aid_int = int(aid)
            except ValueError:
                aid_int = 0
            records.append({
                "agente_id":          aid_int,
                "mes":                mes,
                "nome":               d["nome"],
                "grupo":              d["grupo"],
                "total_tickets":      d["total"],
                "tickets_resolvidos": d["resolvidos"],
                "csat_good":          good,
                "csat_bad":           bad,
                "csat_score":         csat_s,
                "tma_h":              tma,
                "atualizado_em":      now_ts,
            })

        n = _dbl.upsert_batch(sb, "cx_performance_agentes", records, "agente_id,mes")
        log.info(f"cx_performance_agentes → {n} registros upsertados")
        log.info("=== sync_agent_performance: concluído ===")
        return True

    except Exception as e:
        import traceback
        log.error(f"sync_agent_performance falhou: {e}\n{traceback.format_exc()}")
        return False


def upload_ftp():
    try:
        with FTP() as ftp:
            ftp.connect(FTP_HOST, 21, timeout=30)
            ftp.login(FTP_USER, FTP_PASS)
            # dashboard com dados atualizados
            with open(DASHBOARD_COPY, "rb") as f:
                ftp.storbinary(f"STOR {FTP_REMOTE_DASH}", f)
            log.info("FTP: dashboard.html enviado.")
            if PORTAL.exists():
                with open(PORTAL, "rb") as f:
                    ftp.storbinary(f"STOR {FTP_REMOTE_PORTAL}", f)
                log.info("FTP: cx-portal.html (index.html) enviado.")
            if MB_SERVICE.exists():
                with open(MB_SERVICE, "rb") as f:
                    ftp.storbinary(f"STOR {FTP_REMOTE_MB_SERVICE}", f)
                log.info("FTP: services/metabase.js enviado.")
            if MB_DATA_JS.exists():
                with open(MB_DATA_JS, "rb") as f:
                    ftp.storbinary(f"STOR {FTP_REMOTE_MB_DATA}", f)
                log.info("FTP: data/metabase_data.js enviado.")
        log.info("FTP: todos os arquivos enviados para Hostinger com sucesso.")
        return True
    except FTP_ERRORS as e:
        log.error(f"FTP: falha no envio — {e}")
        return False

def _append_log(status: str, ts: str, issues: list[str]):
    line = f"[{ts}] {status}"
    if issues:
        line += "  |  " + "  |  ".join(issues)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _sb_state_get(key: str) -> "str | None":
    """Lê valor do cx_sync_state no Supabase."""
    try:
        import db_loader_metabase as _dbl
        row = _dbl.get_client().table("cx_sync_state").select("value").eq("key", key).execute().data
        return row[0]["value"] if row else None
    except Exception:
        return None


def _sb_state_set(key: str, value: str) -> None:
    """Upserta valor no cx_sync_state no Supabase."""
    try:
        import db_loader_metabase as _dbl
        _dbl.get_client().table("cx_sync_state").upsert(
            {"key": key, "value": value, "atualizado_em": datetime.now(BRT).isoformat()},
            on_conflict="key"
        ).execute()
    except Exception as e:
        log.warning(f"cx_sync_state set '{key}' falhou: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def _refresh_tma_via_rpc() -> None:
    """Chama refresh_cx_tma_semanal() no Supabase — 1 chamada em vez de 26+ HTTP requests."""
    try:
        import db_loader_metabase as _dbl
        _dbl.get_client().rpc("refresh_cx_tma_semanal").execute()
        log.info("cx_tma_semanal: atualizado via RPC Supabase")
    except Exception as e:
        log.warning(f"refresh_cx_tma_semanal RPC falhou, tentando fallback Python: {e}")
        sync_tma_semanal()


def _refresh_agentes_via_rpc() -> None:
    """Chama refresh_cx_performance_agentes() no Supabase — 1 RPC em vez de 26+ paginações HTTP."""
    try:
        import db_loader_metabase as _dbl
        _dbl.get_client().rpc("refresh_cx_performance_agentes").execute()
        log.info("cx_performance_agentes: atualizado via RPC Supabase")
    except Exception as e:
        log.warning(f"refresh_cx_performance_agentes RPC falhou, tentando fallback Python: {e}")
        sync_agent_performance()


def run():
    now_brt = datetime.now(BRT)
    ts      = now_brt.strftime("%d/%m/%Y %H:%M")
    log.info(f"=== Atualizacao iniciada: {ts} ===")

    ok, issues = collect_and_build(save_csv=True)
    if not ok:
        log.error("collect_and_build falhou — pipeline interrompido.")
        _append_log("ERRO", ts, ["collect_and_build falhou"] + issues)
        _sb_state_set("last_sync_error", now_brt.isoformat())
        return

    sync_metabase(save_js=True)
    sync_saude_recompra()
    _refresh_agentes_via_rpc()
    _refresh_tma_via_rpc()

    status_str = "OK" if not issues else f"OK_COM_AVISOS({len(issues)})"
    _append_log(status_str, ts, issues)
    _sb_state_set("last_sync_ok", now_brt.isoformat())
    log.info(f"=== Concluido: {ts} — {status_str} ===")

if __name__ == "__main__":
    run()
