import os

# ── Credenciais ────────────────────────────────────────────────────────────────
ZENDESK_SUBDOMAIN = "appblis"
ZENDESK_EMAIL     = os.getenv("ZENDESK_EMAIL", "")
ZENDESK_TOKEN     = os.getenv("ZENDESK_TOKEN", "")
BASE_URL          = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

# ── Período 2026 ───────────────────────────────────────────────────────────────
START_DATE = "2026-01-01"
END_DATE   = "2026-07-31"

MESES_2026 = {
    "2026-01": "Janeiro",
    "2026-02": "Fevereiro",
    "2026-03": "Março",
    "2026-04": "Abril",
    "2026-05": "Maio",
    "2026-06": "Junho",
    "2026-07": "Julho",
}

# ── Grupos confirmados via API /api/v2/groups.json ─────────────────────────────
GRUPO_CLOUD_HUMANS_ID    = 50304023554451  # IA — Cloud Humans (ativo desde 27/03/2026)
GRUPO_BLIS_SAUDE_ID      = 43771604769299  # Blis Saúde
GRUPO_BLIS_RESOLVE_ID    = 42056691282323  # Blis Resolve (grupo padrão)
GRUPO_BLIS_LOGISTICA_ID  = 48830603080723  # Blis Logística

GRUPO_BLIS_SAUDE      = "Blis Saúde"
GRUPO_BLIS_RESOLVE    = "Blis Resolve"
GRUPO_BLIS_LOGISTICA  = "Blis Logística"
GRUPO_CLOUD_HUMANS    = "Cloud Humans"

# ── Outros grupos existentes ────────────────────────────────────────────────────
# Blis Financeiro:  43287806312851
# Blis Gestão:      50252963716627
# Blis Jurídico:    50252945099667
# Blis Tech:        43287811440403

# ── Tags de identificação (Cloud Humans = grupo, não tag) ──────────────────────
# A IA é identificada pelo group_id = GRUPO_CLOUD_HUMANS_ID
AI_TAGS  = []  # não há tag específica — usar group_id
N2_TAGS  = ["n2", "escalado_n2", "transferido_n2", "nivel_2"]
FCR_TAGS = ["fcr", "resolvido_primeiro_contato", "first_contact_resolution"]

# ── Custom Fields — Motivos / Submotivos / Perfil ──────────────────────────────
CAMPO_MOTIVO_PAI = 43754532953235
CAMPO_PERFIL     = 43754259823123
CAMPOS_SUBMOTIVO = [
    43754673247379, 43754737472019, 43754883922323,
    43754976302099, 43771177081747, 43771237175955,
    43771301635091, 44807136133907, 48166687223315,
]

# tag value → short dashboard ID
# motivo_id → campo de submotivo correto (um campo por categoria)
# Confirmado via campo_opcoes.json: título do campo identifica a categoria
MOTIVO_SUBMOTIVO_FIELD = {
    "appblis":   43754976302099,  # "Motivo bugs appBlis"
    "cadastros": 44807136133907,  # "Motivo Cadastros"
    "medicos":   43771301635091,  # "Motivo Médicos | Tratamentos"
    "docs":      43771237175955,  # "Motivo Documentos"
    "logistica": 43754883922323,  # "Motivos Logística"
    "pagamento": 43754737472019,  # "Motivos Pagamentos"
    "estoque":   43771177081747,  # "Motivos Estoque"
    "receitas":  43754673247379,  # "Motivos Receita | Acompanhamento"
    "cancela":   48166687223315,  # "Motivos Cancelar Tratamento"
}

MOTIVO_TAG_ID = {
    "motivo_contato_appblis":                       "appblis",
    "motivo_contato_blis_doctor":                   "cadastros",
    "motivo_contato_consulta_medicacao_tratamento": "medicos",
    "motivo_contato_documentacao":                  "docs",
    "motivo_contato_entrega_logistica":             "logistica",
    "motivo_contato_pagamento":                     "pagamento",
    "motivo_contato_produtos_marcas":               "estoque",
    "motivo_contato_receita_anvisa":                "receitas",
    "motivo_cancelamento":                          "cancela",
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
