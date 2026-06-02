-- Analytics de Clientes — Novas tabelas Supabase
-- Executar no Supabase SQL Editor (Settings → SQL Editor)

-- 1. KPIs globais de clientes (snapshot diário)
CREATE TABLE IF NOT EXISTS mb_clientes_resumo (
    data_carga               DATE PRIMARY KEY,
    clientes_unicos          INTEGER,
    total_recompras_periodo  INTEGER
);

-- 2. Análise por protocolo
CREATE TABLE IF NOT EXISTS mb_protocolo_analise (
    protocolo        TEXT PRIMARY KEY,
    qtd_recompras    INTEGER,
    total_usuarios   INTEGER,
    pct_recompra     NUMERIC(8,4),
    soma_receita     NUMERIC(16,2),
    qtd_pedidos      INTEGER,
    ticket_medio     NUMERIC(12,2),
    qtd_consultas    INTEGER,
    data_carga       DATE
);

-- 3. Análise por safra (coorte)
CREATE TABLE IF NOT EXISTS mb_safra_analise (
    safra            TEXT PRIMARY KEY,  -- YYYY-MM
    total_usuarios   INTEGER,
    com_recompra     INTEGER,
    pct_recompra     NUMERIC(8,4),
    media_pedidos    NUMERIC(8,2),
    data_carga       DATE
);

-- 4. Distribuição por tipo de cliente
CREATE TABLE IF NOT EXISTS mb_tipo_cliente (
    tipo_cliente     TEXT PRIMARY KEY,
    qtd_usuarios     INTEGER,
    pct_do_total     NUMERIC(8,4),
    media_pedidos    NUMERIC(8,2),
    data_carga       DATE
);

-- 5. Evolução mensal de recompras
CREATE TABLE IF NOT EXISTS mb_recompra_mensal (
    periodo          TEXT PRIMARY KEY,  -- YYYY-MM-DD (primeiro dia do mês)
    qtd_recompras    INTEGER,
    consultas_criadas INTEGER,
    pedidos_recompra  INTEGER,
    pedidos          INTEGER,
    pct_recompra     NUMERIC(8,4),
    data_carga       DATE
);

-- Habilitar RLS com leitura pública (anon key)
ALTER TABLE mb_clientes_resumo   ENABLE ROW LEVEL SECURITY;
ALTER TABLE mb_protocolo_analise ENABLE ROW LEVEL SECURITY;
ALTER TABLE mb_safra_analise     ENABLE ROW LEVEL SECURITY;
ALTER TABLE mb_tipo_cliente      ENABLE ROW LEVEL SECURITY;
ALTER TABLE mb_recompra_mensal   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read" ON mb_clientes_resumo   FOR SELECT USING (true);
CREATE POLICY "anon_read" ON mb_protocolo_analise FOR SELECT USING (true);
CREATE POLICY "anon_read" ON mb_safra_analise     FOR SELECT USING (true);
CREATE POLICY "anon_read" ON mb_tipo_cliente      FOR SELECT USING (true);
CREATE POLICY "anon_read" ON mb_recompra_mensal   FOR SELECT USING (true);
