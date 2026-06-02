"""
updater.py — Atualização diária do dashboard_2026.html a partir dos CSVs do Zendesk.

Fluxo:
  1. Roda extractor_2026.py  (busca dados reais da API)
  2. Lê output/tabela_tickets.csv  e  output/tabela_csat.csv
  3. Computa todos os blocos de D (tickets, status, channels, semanas, csat)
  4. Valida integridade cruzada antes de injetar
  5. Injeta const D atualizado no HTML — sem tocar em CSAT comments, motivos ou sub
  6. Grava log em output/update_log.txt
  7. Em falha: restaura backup e registra erro

Causa raiz de dados errados:
  Execute UMA VEZ com o token real:
      set ZENDESK_TOKEN=SEU_TOKEN_AQUI
      python updater.py
  Depois o Task Scheduler mantém tudo atualizado às 09:00 automaticamente.
"""

from dotenv import load_dotenv
load_dotenv()

import os, subprocess, sys, re, json, shutil, logging, urllib.request, urllib.error
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
    "pedidosProtocolo":     445,
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
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

MONTH_IDX = {"2026-01":0,"2026-02":1,"2026-03":2,"2026-04":3,"2026-05":4}
N_MONTHS  = 5

# Semanas ISO 2026 mapeadas para mês (0=Jan ... 4=Mai)
# Semana começa segunda. Critério: maioria dos dias no mês.
WEEK_TO_MONTH = {
    1:0, 2:0, 3:0, 4:0,           # Jan: S01-S04
    5:1, 6:1, 7:1, 8:1, 9:1,      # Fev: S05-S09
    10:2,11:2,12:2,13:2,           # Mar: S10-S13
    14:3,15:3,16:3,17:3,18:3,      # Abr: S14-S18
    19:4,20:4,21:4,                # Mai: S19-S21
}

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

# ── Auth / extração ───────────────────────────────────────────────────────────

def run_extractor() -> bool:
    log.info("Rodando extractor_2026.py...")
    try:
        r = subprocess.run(
            [sys.executable, str(BASE / "extractor_2026.py")],
            capture_output=True, text=True, timeout=1800,
            cwd=str(BASE),
        )
        if r.returncode != 0:
            log.error(f"Extractor saiu com erro:\n{r.stderr[-2000:]}")
            return False
        log.info("Extracao OK.")
        return True
    except subprocess.TimeoutExpired:
        log.error("Extractor excedeu 10 min de timeout.")
        return False
    except Exception as e:
        log.error(f"Erro ao rodar extractor: {e}")
        return False

# ── Leitura dos CSVs ──────────────────────────────────────────────────────────

def load_csvs():
    paths = {
        "tickets": OUT / "tabela_tickets.csv",
        "csat":    OUT / "tabela_csat.csv",
    }
    for k, p in paths.items():
        if not p.exists():
            raise FileNotFoundError(f"CSV nao encontrado: {p}")
    df_t = pd.read_csv(paths["tickets"])
    df_c = pd.read_csv(paths["csat"])
    log.info(f"tickets={len(df_t)}  csat={len(df_c)}")
    return df_t, df_c

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

TEAM_OF_TIME = {"Blis Saúde":"saude","Blis Resolve":"resolve","Outros":"resolve"}

def _team(row) -> str:
    v = row.get("atendido_por_ia")
    if v is True or str(v).lower() == "true":
        return "ia"
    return TEAM_OF_TIME.get(str(row.get("time","")), "resolve")

# ── Construção dos blocos ─────────────────────────────────────────────────────

def build_tickets(df: pd.DataFrame) -> dict:
    out = {t: [0]*N_MONTHS for t in ("ia","saude","resolve")}
    for _, r in df.iterrows():
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is not None:
            out[_team(r)][m] += 1
    return out

def build_status(df: pd.DataFrame) -> dict:
    out = {t: {"closed":0,"solved":0,"open":0,"pending":0,"new_":0}
           for t in ("ia","saude","resolve")}
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
    out = {"ia":{},"saude":{},"resolve":{}}
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
    out = {t: [{} for _ in range(N_MONTHS)] for t in ("ia","saude","resolve")}
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
    teams = ("ia","saude","resolve")

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
        "labels": labels,
        "datas":  datas,
        "mesIdx": mes_idx,
        "ia":     weekly["ia"],
        "saude":  weekly["saude"],
        "resolve":weekly["resolve"],
    }

def build_csat(df_c: pd.DataFrame, df_t: pd.DataFrame) -> dict:
    out = {t: {"good":[0]*N_MONTHS,"bad":[0]*N_MONTHS}
           for t in ("ia","saude","resolve")}
    # Para Saúde e Resolve: usa o campo `time` direto do CSAT (mais preciso,
    # sem depender de ticket_id presente em tabela_tickets).
    # Para IA: cross-referência com tabela_tickets (atendido_por_ia=True),
    # pois o CSAT agrupa IA junto com "Outros" sem distinção.
    ia_ticket_ids: set = set()
    for _, r in df_t.iterrows():
        if pd.isna(r.get("ticket_id")):
            continue
        is_ia = r.get("atendido_por_ia")
        is_ia = is_ia is True or str(is_ia).lower() == "true"
        if is_ia:
            ia_ticket_ids.add(str(r["ticket_id"]))
    for _, r in df_c.iterrows():
        score = str(r.get("score_raw",""))
        if score not in ("good","bad"):
            continue
        m = MONTH_IDX.get(str(r.get("ano_mes","")))
        if m is None:
            continue
        time_val = str(r.get("time",""))
        if "Saúde" in time_val or "Saude" in time_val or time_val == "saude":
            out["saude"][score][m] += 1
        elif "Resolve" in time_val or time_val == "resolve":
            out["resolve"][score][m] += 1
        elif str(r.get("ticket_id","")) in ia_ticket_ids:
            out["ia"][score][m] += 1
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
    out = {t: [empty() for _ in range(N_MONTHS)] for t in ("ia","saude","resolve")}
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
    out = {t: [0]*N_MONTHS for t in ("ia","saude","resolve")}
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
        monthly = [0]*N_MONTHS; ia=[0]*N_MONTHS; saude=[0]*N_MONTHS; resolve=[0]*N_MONTHS
        for (team, m), grp in sub.groupby(["_team","_m"]):
            n = len(grp)
            monthly[m] += n
            if team == "ia":      ia[m]      += n
            elif team == "saude": saude[m]   += n
            else:                 resolve[m] += n
        motivos.append({"id":motivo_id,"nome":label,"monthly":monthly,"resolve":resolve,"saude":saude,"ia":ia})
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
    ia_c: list[int] = []; saude_c: list[int] = []; resolve_c: list[int] = []
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
                    "total": len(msub),
                    "ia":      int(mtc.get("ia", 0)),
                    "saude":   int(mtc.get("saude", 0)),
                    "resolve": int(mtc.get("resolve", 0)),
                }
            counts_per_perfil[perfil_tag] = counts
        else:
            motivos_per_perfil[perfil_tag] = []

    log.info(f"Perfis encontrados: {labels}")
    return {"labels": labels, "ids": ids, "ia": ia_c, "saude": saude_c, "resolve": resolve_c,
            "motivos": motivos_per_perfil, "counts": counts_per_perfil}


# ── Validação cruzada ─────────────────────────────────────────────────────────

def cross_check(tickets: dict, status: dict, channels: dict,
                semanas: dict) -> list[str]:
    issues = []
    for t in ("ia","saude","resolve"):
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

    TEAM_LABEL = {"resolve": "resolve", "saude": "saude"}

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

# ── Serialização JS ───────────────────────────────────────────────────────────

def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",",":"))

def build_data_js(tickets, status, status_monthly, channels, channels_monthly, semanas, csat, tempos,
                  motivos_data, sub_data, perfis_data, n2_data) -> str:
    now = datetime.now(BRT).strftime("%d/%m/%Y %H:%M")
    tempos_js  = f"  tempos:{_js(tempos)},"  if tempos      else "  // tempos: nao disponivel"
    perfis_js  = f"  perfis:{_js(perfis_data)}," if perfis_data else "  perfis:{},"
    motivos_js = f"  motivos:{_js(motivos_data)}," if motivos_data else "  motivos:[],"
    sub_js     = f"  sub:{_js(sub_data)},"    if sub_data    else "  sub:{},"
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
{sub_js}"""

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

    MONTH_IDX = {"2026-01":0,"2026-02":1,"2026-03":2,"2026-04":3,"2026-05":4}
    has_com = df_c["comentario"].fillna("").str.strip() != ""
    scored  = df_c[df_c["score_raw"].isin(["good","bad"]) & has_com].copy()
    scored["m"]    = scored["ano_mes"].map(MONTH_IDX)
    scored = scored.dropna(subset=["m"]); scored["m"] = scored["m"].astype(int)
    scored["team"] = scored["time"].apply(_team)
    scored["txt"]  = scored["comentario"].apply(_clean)
    scored = scored[scored["txt"].str.len() >= 10]

    def _sample(grp, n=30):
        rows = grp.to_dict("records")
        return _rnd.sample(rows, n) if len(rows) > n else rows

    bad_all=[]; good_all=[]
    for m in range(5):
        sub = scored[scored["m"] == m]
        for r in _sample(sub[sub["score_raw"]=="bad"],  30):
            bad_all.append({"m":m,"team":r["team"],"t":r["txt"]})
        for r in _sample(sub[sub["score_raw"]=="good"], 30):
            good_all.append({"m":m,"team":r["team"],"t":r["txt"]})

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
    for m in range(5):
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

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    now_brt = datetime.now(BRT)
    ts      = now_brt.strftime("%d/%m/%Y %H:%M")
    log.info(f"=== Atualizacao iniciada: {ts} ===")

    if DASHBOARD.exists():
        shutil.copy2(DASHBOARD, BACKUP)

    # 1. Extração
    if not run_extractor():
        log.error("Extractor falhou — dataset mantido.")
        _append_log("ERRO", ts, ["Extractor falhou"])
        return

    # 2. Leitura
    try:
        df_t, df_c = load_csvs()
    except Exception as e:
        log.error(f"Erro CSV: {e}")
        _append_log("ERRO", ts, [str(e)])
        return

    # 3. Filtro: remove disparos de CRM do Blis Saúde (tickets sem assignee real)
    mask_crm = (df_t["time"] == "Blis Saúde") & (
        df_t["assignee_id"].isna() |
        (df_t["assignee_id"].astype(str).str.strip().isin(["", "nan"]))
    )
    n_crm = mask_crm.sum()
    if n_crm:
        df_t = df_t[~mask_crm].copy()
        log.info(f"Filtro CRM Blis Saude: {n_crm} sem assignee removidos -> {(df_t['time']=='Blis Saude').sum()} restantes")

    # 3b. Validação
    issues = validate_csv(df_t)
    for i in issues:
        log.warning(i)

    # 4. Construção
    tickets      = build_tickets(df_t)
    status       = build_status(df_t)
    channels          = build_channels(df_t)
    channels_monthly  = build_channels_monthly(df_t)
    semanas           = build_semanas(df_t)
    csat              = build_csat(df_c, df_t)
    tempos            = build_tempos(df_t)
    n2_data           = build_n2(df_t)
    status_monthly    = build_status_monthly(df_t)
    tag_names    = load_opcoes()
    motivos_data = build_motivos_data(df_t)
    sub_data     = build_sub_data(df_t, tag_names)
    perfis_data  = build_perfis_data(df_t)

    # 5. Validação cruzada
    cross = cross_check(tickets, status, channels, semanas)
    for c in cross:
        log.warning(f"CROSS: {c}")
    issues.extend(cross)

    # Log dos totais
    for t in ("ia","saude","resolve"):
        log.info(f"  {t}: tickets={sum(tickets[t])}  status={sum(status[t].values())}  channels={sum(channels[t].values())}")
    if tempos:
        log.info(f"  tempos: TMA Resolve={tempos['tma']['resolve']}  TMA Saude={tempos['tma']['saude']}")
    if motivos_data:
        log.info(f"  motivos: {len(motivos_data)} categorias  perfis: {len(perfis_data.get('labels',[]))}")

    # 6. Injeção
    try:
        html = DASHBOARD.read_text(encoding="utf-8")
        data_js = build_data_js(tickets, status, status_monthly, channels, channels_monthly, semanas, csat, tempos,
                                 motivos_data, sub_data, perfis_data, n2_data)
        html = inject_into_html(html, data_js)
        html = update_sync_ts(html, ts)
        # 6b. Comentários e Offenders CSAT (dados reais do CSV)
        try:
            comments, offenders = build_comments_offenders(df_c)
            html = inject_comments(html, comments, offenders)
            log.info(f"CSAT comments: {len(comments['bad'])} detratores, {len(comments['good'])} promotores injetados.")
        except Exception as ec:
            log.warning(f"Comentários CSAT nao injetados: {ec}")
        DASHBOARD.write_text(html, encoding="utf-8")
        shutil.copy2(DASHBOARD, DASHBOARD_COPY)   # atualiza cópia usada pelo portal
        log.info(f"Dashboard atualizado com sucesso.")
    except Exception as e:
        log.error(f"Erro na injecao: {e}")
        if BACKUP.exists():
            shutil.copy2(BACKUP, DASHBOARD)
            log.info("Backup restaurado.")
        _append_log("ERRO", ts, [str(e)])
        return

    # 7. Dados Metabase (para o Dash Médicos no portal)
    fetch_metabase_data()

    # 8. Envio FTP para Hostinger
    ftp_ok = upload_ftp()

    status_str = "OK" if not issues else f"OK_COM_AVISOS({len(issues)})"
    if not ftp_ok:
        status_str += "_FTP_FALHOU"
    _append_log(status_str, ts, issues)
    log.info(f"=== Concluido: {ts} — {status_str} ===")

def fetch_metabase_data():
    """Autentica no Metabase, busca todos os cards e salva como JS para o portal."""
    def mb_post(path, body=None, token=None):
        url  = f"{METABASE_URL}{path}"
        data = json.dumps(body or {}).encode()
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["X-Metabase-Session"] = token
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

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
        for key, card_id in MB_CARD_IDS.items():
            try:
                resp  = mb_post(f"/api/card/{card_id}/query", {}, token=token)
                result[key] = compact(resp, MB_ROW_LIMITS.get(key))
                log.info(f"Metabase card {card_id} ({key}): OK ({len(result[key]['data']['rows'])} linhas)")
            except Exception as e:
                log.warning(f"Metabase card {card_id} ({key}): {e}")
        # couponsGasto — agregação completa do card 448 (export sem limite de 2000 linhas)
        try:
            rows_export = mb_export_json(448, token)
            result["couponsGasto"] = aggregate_gasto(rows_export)
            log.info(f"Metabase couponsGasto: OK ({len(rows_export)} pedidos, {len(result['couponsGasto']['data']['rows'])} cupons)")
        except Exception as e:
            log.warning(f"Metabase couponsGasto: {e}")

        # baseRecompra — card 404: dados completos de recompra por cliente (agregados + métricas avançadas)
        try:
            from collections import defaultdict
            from datetime import date as _date
            rows_export_404 = mb_export_json(404, token)
            today_d = _date.today()
            safra_map = defaultdict(lambda: {
                'total': 0, 'com_recompra': 0, 'total_pedidos': 0,
                'dias_1_2_sum': 0, 'dias_1_2_cnt': 0,
                'ativou_90d': 0, 'inativos': 0,
            })
            tipo_map  = defaultdict(lambda: {'total': 0, 'total_pedidos': 0, 'receita': 0.0})
            TIPOS = [('Novo', 1, 1), ('Recorrente', 2, 3), ('Fiel', 4, 6), ('Alta Frequência', 7, 9999)]
            user_tipo = {}  # user_id → tipo (para join com card 203)

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

                    # Dias até segunda compra
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

                    # Inativos: 1 pedido + primeira compra > 90 dias atrás
                    if tot == 1 and len(d1) == 10:
                        try:
                            age = (today_d - _date.fromisoformat(d1)).days
                            if age > 90:
                                sd['inativos'] += 1
                        except Exception:
                            pass

                tipo_map[tipo]['total']        += 1
                tipo_map[tipo]['total_pedidos'] += tot

            # Safra card
            safra_cols = [{'name': c} for c in [
                'safra','total_usuarios','com_recompra','pct_recompra','media_pedidos',
                'avg_dias_1_2','inativos','pct_ativou_90d',
            ]]
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
                ])
            result['safraAnalise'] = {'data': {'cols': safra_cols, 'rows': safra_rows}}

            # Receita por tipo — card 203 (RFM base)
            try:
                rows_203 = mb_export_json(203, token)
                for r203 in rows_203:
                    st  = str(r203.get('status') or '').lower()
                    if 'cancelad' in st or 'reembolsad' in st:
                        continue
                    uid203  = str(r203.get('User_id') or r203.get('user_id') or '')
                    total203 = float(r203.get('Total') or r203.get('total') or 0)
                    if uid203 and total203 > 0:
                        t = user_tipo.get(uid203)
                        if t:
                            tipo_map[t]['receita'] += total203
                log.info(f"Metabase card 203: {len(rows_203)} pedidos processados para ticket_medio")
            except Exception as e:
                log.warning(f"Metabase card 203 (receita): {e}")

            # Tipo cliente card (com receita/ticket_medio)
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

            # KPIs globais de comportamento
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

        # intervaloPedidos — cards 82+191: média de dias entre pedidos
        try:
            r82  = mb_post(f"/api/card/82/query",  {}, token=token)
            r191 = mb_post(f"/api/card/191/query", {}, token=token)
            rows82  = r82.get('data',{}).get('rows',[])
            rows191 = r191.get('data',{}).get('rows',[])
            avg12 = rows82[0][1]  if rows82  else None
            avg23 = rows191[0][2] if rows191 else None
            ik_cols = [{'name': c} for c in ['avg_dias_1_2','avg_dias_2_3']]
            result['intervaloPedidos'] = {'data': {'cols': ik_cols, 'rows': [[avg12, avg23]]}}
            log.info(f"Metabase intervaloPedidos: 1→2={avg12:.1f}d  2→3={avg23:.1f}d" if avg12 else "Metabase intervaloPedidos: sem dados")
        except Exception as e:
            log.warning(f"Metabase intervaloPedidos (82/191): {e}")

        # churnMensal — card 209: agrega churn por mês
        try:
            rows_209 = mb_export_json(209, token)
            churn_map = defaultdict(int)
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

        ts_brt = datetime.now(tz=timezone(timedelta(hours=-3))).strftime("%Y-%m-%dT%H:%M:%S")
        js = f"/* Metabase preloaded — {ts_brt} BRT */\nwindow.MB_PRELOADED={json.dumps(result, ensure_ascii=False, separators=(',',':'))};\n"
        MB_DATA_JS.write_text(js, encoding="utf-8")
        log.info(f"Metabase: {MB_DATA_JS} salvo ({MB_DATA_JS.stat().st_size:,} bytes)")
        return True
    except Exception as e:
        log.error(f"Metabase fetch falhou: {e}")
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

if __name__ == "__main__":
    run()
