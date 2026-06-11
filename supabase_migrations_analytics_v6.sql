-- supabase_migrations_analytics_v6.sql
-- Tabelas de performance por agente de suporte (Blis Resolve + Blis Saúde)
-- Alimentadas por sync_agent_performance() em updater.py

CREATE TABLE IF NOT EXISTS cx_performance_agentes (
    agente_id           BIGINT      NOT NULL,
    mes                 TEXT        NOT NULL,   -- "YYYY-MM"
    nome                TEXT,
    grupo               TEXT,                   -- 'resolve' | 'saude'
    total_tickets       INT         DEFAULT 0,
    tickets_resolvidos  INT         DEFAULT 0,
    csat_good           INT         DEFAULT 0,
    csat_bad            INT         DEFAULT 0,
    csat_score          NUMERIC(5,2),
    tma_h               NUMERIC(6,1),
    fcr_pct             NUMERIC(5,2),
    atualizado_em       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agente_id, mes)
);

CREATE INDEX IF NOT EXISTS idx_cx_perf_mes   ON cx_performance_agentes(mes);
CREATE INDEX IF NOT EXISTS idx_cx_perf_grupo ON cx_performance_agentes(grupo);

-- View consolidada para leitura no dashboard
CREATE OR REPLACE VIEW cx_performance_view AS
SELECT
    agente_id,
    nome,
    grupo,
    mes,
    total_tickets,
    tickets_resolvidos,
    csat_good,
    csat_bad,
    csat_score,
    ROUND(tickets_resolvidos::numeric / NULLIF(total_tickets, 0) * 100, 1) AS taxa_resolucao_pct,
    tma_h,
    fcr_pct,
    atualizado_em
FROM cx_performance_agentes
ORDER BY grupo, mes DESC, total_tickets DESC;

-- RLS
ALTER TABLE cx_performance_agentes ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read_cx_perf"
    ON cx_performance_agentes FOR SELECT
    TO anon USING (true);

CREATE POLICY "service_write_cx_perf"
    ON cx_performance_agentes FOR ALL
    TO service_role USING (true) WITH CHECK (true);
