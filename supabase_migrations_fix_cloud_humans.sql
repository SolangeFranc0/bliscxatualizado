-- Corrige tickets históricos do Cloud Humans que ficaram com atendido_por_ia = false.
-- Executar UMA VEZ no Supabase SQL Editor.
-- Após executar, disparar o workflow manualmente para recalcular cx_volume_diario.

UPDATE tickets
SET atendido_por_ia = true
WHERE group_id = 50304023554451   -- Cloud Humans (GRUPO_CLOUD_HUMANS_ID)
  AND (atendido_por_ia IS NULL OR atendido_por_ia = false);

-- Verificação: deve retornar todos os tickets Cloud Humans com atendido_por_ia = true
SELECT ano_mes, count(*) AS total
FROM tickets
WHERE group_id = 50304023554451
GROUP BY ano_mes
ORDER BY ano_mes;
