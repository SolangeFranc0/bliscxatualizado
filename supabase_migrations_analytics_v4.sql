-- Analytics v4 — Receita por protocolo por mês
-- Cole e execute no Supabase: https://supabase.com/dashboard/project/duzqjnbtxufhkozyclqk/sql/new

CREATE TABLE IF NOT EXISTS mb_protocolo_mensal (
    periodo       DATE        NOT NULL,  -- 2026-01-01
    protocolo     TEXT        NOT NULL,
    soma_receita  DECIMAL(15,2),
    qtd_pedidos   INTEGER,
    ticket_medio  DECIMAL(15,2),
    data_carga    DATE,
    PRIMARY KEY (periodo, protocolo)
);

ALTER TABLE mb_protocolo_mensal ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'mb_protocolo_mensal' AND policyname = 'anon_read'
  ) THEN
    EXECUTE 'CREATE POLICY anon_read ON mb_protocolo_mensal FOR SELECT USING (true)';
  END IF;
END $$;

-- Verificação: deve retornar a tabela criada
SELECT tablename FROM pg_tables
WHERE schemaname = 'public'
  AND tablename = 'mb_protocolo_mensal';
