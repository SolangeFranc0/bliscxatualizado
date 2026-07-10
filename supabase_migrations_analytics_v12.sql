-- v12: adiciona colunas n_1/n_2/n_3/n_4plus na tabela mb_safra_analise
-- Representa quantos clientes de cada safra ficaram em 1, 2, 3 ou 4+ compras

ALTER TABLE mb_safra_analise
  ADD COLUMN IF NOT EXISTS n_1     INT,
  ADD COLUMN IF NOT EXISTS n_2     INT,
  ADD COLUMN IF NOT EXISTS n_3     INT,
  ADD COLUMN IF NOT EXISTS n_4plus INT;

COMMENT ON COLUMN mb_safra_analise.n_1     IS 'Clientes da safra que fizeram exatamente 1 compra';
COMMENT ON COLUMN mb_safra_analise.n_2     IS 'Clientes da safra que fizeram exatamente 2 compras';
COMMENT ON COLUMN mb_safra_analise.n_3     IS 'Clientes da safra que fizeram exatamente 3 compras';
COMMENT ON COLUMN mb_safra_analise.n_4plus IS 'Clientes da safra que fizeram 4 ou mais compras';
