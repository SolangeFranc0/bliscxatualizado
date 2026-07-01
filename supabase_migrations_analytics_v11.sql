-- v11: adiciona ticket_id na tabela comentarios_csat
ALTER TABLE comentarios_csat
  ADD COLUMN IF NOT EXISTS ticket_id BIGINT;
