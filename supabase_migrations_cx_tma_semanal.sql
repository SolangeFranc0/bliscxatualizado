-- Tabela de TMA semanal por agente (Blis Resolve + Blis Saúde)
-- Populada pelo updater.py → sync_tma_semanal()
-- Mantém as últimas N semanas; upsert por (agente_id, semana)

CREATE TABLE IF NOT EXISTS cx_tma_semanal (
  agente_id    BIGINT       NOT NULL,
  semana       TEXT         NOT NULL,  -- ex: "2026-W29"
  nome         TEXT,
  grupo        TEXT,                   -- "resolve" ou "saude"
  tma_h        FLOAT,
  tmr_h        FLOAT,                  -- tempo de primeiro atendimento (horas úteis)
  n_tickets    INT,
  atualizado_em TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (agente_id, semana)
);

-- Adiciona tmr_h em tabelas existentes (seguro executar múltiplas vezes)
ALTER TABLE cx_tma_semanal ADD COLUMN IF NOT EXISTS tmr_h FLOAT;

-- Permite leitura pública via anon key (mesma política dos demais)
ALTER TABLE cx_tma_semanal ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read" ON cx_tma_semanal FOR SELECT USING (true);
