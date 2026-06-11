-- supabase_migrations_analytics_v5.sql
-- Tabela de saída: cohort mensal de recompra de clientes Blis Saúde
-- Alimentada por sync_saude_recompra() em updater.py

CREATE TABLE IF NOT EXISTS mb_saude_recompra (
    mes                      TEXT        PRIMARY KEY,  -- "YYYY-MM"
    total_contatos_saude     INT,
    clientes_com_app         INT,
    clientes_recompraram     INT,
    taxa_recompra_pct        NUMERIC(5,2),
    taxa_recompra_geral_pct  NUMERIC(5,2),
    receita_recompradores    NUMERIC(12,2),
    atualizado_em            TIMESTAMPTZ DEFAULT NOW()
);

-- RLS
ALTER TABLE mb_saude_recompra ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read_mb_saude_recompra"
    ON mb_saude_recompra FOR SELECT
    TO anon USING (true);

CREATE POLICY "service_write_mb_saude_recompra"
    ON mb_saude_recompra FOR ALL
    TO service_role USING (true) WITH CHECK (true);
