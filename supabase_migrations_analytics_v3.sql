-- Analytics v3 — Frequência de compras por cliente
-- Cole e execute no Supabase: https://supabase.com/dashboard/project/duzqjnbtxufhkozyclqk/sql/new

CREATE TABLE IF NOT EXISTS mb_cohort_pedidos (
    faixa_pedidos  TEXT PRIMARY KEY,   -- "1 pedido", "2 pedidos", "3 pedidos", "4 pedidos", "Mais de 5 pedidos"
    qtd_usuarios   INTEGER,
    data_carga     DATE
);

ALTER TABLE mb_cohort_pedidos ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'mb_cohort_pedidos' AND policyname = 'anon_read'
  ) THEN
    EXECUTE 'CREATE POLICY anon_read ON mb_cohort_pedidos FOR SELECT USING (true)';
  END IF;
END $$;

-- Verificação: deve retornar as tabelas criadas
SELECT tablename FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('mb_cohort_pedidos', 'mb_tipo_cliente', 'mb_protocolo_analise');
