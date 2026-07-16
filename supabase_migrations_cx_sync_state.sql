-- Estado do pipeline e cursor incremental Zendesk
CREATE TABLE IF NOT EXISTS cx_sync_state (
  key           TEXT PRIMARY KEY,
  value         TEXT,
  atualizado_em TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE cx_sync_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY "public read" ON cx_sync_state FOR SELECT USING (true);

-- Chaves usadas pelo updater.py:
--   zendesk_cursor   → Unix timestamp do último sync Zendesk bem-sucedido
--   last_sync_ok     → ISO timestamp do último run completo com sucesso
--   last_sync_error  → ISO timestamp do último run que falhou

-- ─────────────────────────────────────────────────────────────────────────────
-- Função: computa TMA semanal das últimas 8 semanas diretamente no banco.
-- Elimina a necessidade de paginar ~26k tickets via HTTP no Python.
-- Chamada por: sb.rpc('refresh_cx_tma_semanal').execute()
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION refresh_cx_tma_semanal()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_recent_8 TEXT[];
BEGIN
  -- Semanas com dados válidos (últimas 8)
  SELECT ARRAY(
    SELECT DISTINCT semana_iso
    FROM tickets
    WHERE semana_iso IS NOT NULL
      AND semana_iso <> ''
      AND resolucao_h > 0
      AND resolucao_h < 720
      AND group_id IN (42056691282323, 43771604769299)
    ORDER BY semana_iso DESC
    LIMIT 8
  ) INTO v_recent_8;

  -- Upsert TMA agregado
  INSERT INTO cx_tma_semanal (agente_id, semana, nome, grupo, tma_h, n_tickets, atualizado_em)
  SELECT
    assignee_id::BIGINT,
    semana_iso,
    nome_agente,
    CASE group_id::BIGINT
      WHEN 42056691282323 THEN 'resolve'
      WHEN 43771604769299 THEN 'saude'
    END,
    ROUND(AVG(resolucao_h)::NUMERIC, 1)::FLOAT,
    COUNT(*)::INT,
    NOW()
  FROM tickets
  WHERE semana_iso = ANY(v_recent_8)
    AND group_id::BIGINT IN (42056691282323, 43771604769299)
    AND resolucao_h > 0
    AND resolucao_h < 720
    AND assignee_id IS NOT NULL
    AND nome_agente IS NOT NULL
    AND nome_agente NOT IN ('', 'None', 'Admin', 'Logística Agentes', 'Roberto venzi pires')
  GROUP BY assignee_id, semana_iso, nome_agente, group_id
  ON CONFLICT (agente_id, semana) DO UPDATE SET
    nome          = EXCLUDED.nome,
    grupo         = EXCLUDED.grupo,
    tma_h         = EXCLUDED.tma_h,
    n_tickets     = EXCLUDED.n_tickets,
    atualizado_em = EXCLUDED.atualizado_em;
END;
$$;

REVOKE ALL ON FUNCTION refresh_cx_tma_semanal() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION refresh_cx_tma_semanal() TO service_role, postgres;
