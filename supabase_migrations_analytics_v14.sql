-- v14: cx_analytics_snapshot — snapshot diário de KPIs analíticos (Metabase)
-- Criado para persistir métricas-chave do dash de Análises no banco de dados.

CREATE TABLE IF NOT EXISTS cx_analytics_snapshot (
  data              date        PRIMARY KEY,
  total_clientes    bigint,
  total_safras      int,
  taxa_recompra_pct numeric(6,2),
  avg_dias_1_2      numeric(6,1),
  inativos          bigint,
  total_churn       bigint,
  atualizado_em     timestamptz DEFAULT now()
);

-- Índice para consultas por período
CREATE INDEX IF NOT EXISTS idx_cx_analytics_snapshot_data ON cx_analytics_snapshot(data DESC);

-- RLS: leitura pública (anon key suficiente para o dashboard)
ALTER TABLE cx_analytics_snapshot ENABLE ROW LEVEL SECURITY;
CREATE POLICY IF NOT EXISTS "cx_analytics_snapshot_read" ON cx_analytics_snapshot
  FOR SELECT USING (true);
CREATE POLICY IF NOT EXISTS "cx_analytics_snapshot_upsert" ON cx_analytics_snapshot
  FOR ALL USING (true);
