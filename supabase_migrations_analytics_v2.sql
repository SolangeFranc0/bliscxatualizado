-- Analytics v2 — Novas tabelas e colunas
-- Executar no Supabase SQL Editor (Settings → SQL Editor)

-- ─── 1. Estender tabelas existentes ───────────────────────────────────────────

ALTER TABLE mb_safra_analise
  ADD COLUMN IF NOT EXISTS avg_dias_1_2  NUMERIC(8,2),
  ADD COLUMN IF NOT EXISTS inativos      INTEGER,
  ADD COLUMN IF NOT EXISTS pct_ativou_90d NUMERIC(8,4);

ALTER TABLE mb_tipo_cliente
  ADD COLUMN IF NOT EXISTS ticket_medio  NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS receita_total NUMERIC(16,2);

-- ─── 2. Novas tabelas ─────────────────────────────────────────────────────────

-- KPIs globais de comportamento de compra
CREATE TABLE IF NOT EXISTS mb_comportamento_kpis (
    id               TEXT PRIMARY KEY DEFAULT 'global',
    avg_dias_1_2     NUMERIC(8,2),
    avg_dias_2_3     NUMERIC(8,2),
    pct_ativou_90d   NUMERIC(8,4),
    inativos_global  INTEGER,
    data_carga       DATE
);

-- Churn mensal (card 209 agregado)
CREATE TABLE IF NOT EXISTS mb_churn_mensal (
    churn_mes        TEXT PRIMARY KEY,  -- YYYY-MM
    qtd_churn        INTEGER,
    data_carga       DATE
);

-- Funil por canal de aquisição (card 282)
CREATE TABLE IF NOT EXISTS mb_funil_canal (
    canal            TEXT PRIMARY KEY,
    pre_cadastro     INTEGER,
    cadastro         INTEGER,
    consulta         INTEGER,
    pedido           INTEGER,
    recompra         INTEGER,
    pct_consulta     NUMERIC(8,4),
    pct_pedido       NUMERIC(8,4),
    pct_recompra     NUMERIC(8,4),
    data_carga       DATE
);

-- ─── 3. RLS ──────────────────────────────────────────────────────────────────

ALTER TABLE mb_comportamento_kpis ENABLE ROW LEVEL SECURITY;
ALTER TABLE mb_churn_mensal       ENABLE ROW LEVEL SECURITY;
ALTER TABLE mb_funil_canal        ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read" ON mb_comportamento_kpis FOR SELECT USING (true);
CREATE POLICY "anon_read" ON mb_churn_mensal       FOR SELECT USING (true);
CREATE POLICY "anon_read" ON mb_funil_canal        FOR SELECT USING (true);
