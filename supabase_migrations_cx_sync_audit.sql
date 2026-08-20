-- cx_sync_audit: histórico de cada execução do pipeline
-- Permite comparar Zendesk ↔ Supabase ↔ Dashboard e detectar onde ocorre divergência.

CREATE TABLE IF NOT EXISTS public.cx_sync_audit (
    sync_id           uuid          PRIMARY KEY DEFAULT gen_random_uuid(),
    iniciado_em       timestamptz   NOT NULL DEFAULT now(),
    concluido_em      timestamptz,
    status            text          NOT NULL DEFAULT 'running',  -- 'ok', 'parcial', 'erro'
    modo              text,                                       -- 'incremental', 'completo'
    cursor_anterior   bigint,
    cursor_novo       bigint,
    paginas_tickets   int,
    tickets_recebidos int,          -- len(tickets_raw) da API Zendesk
    tickets_supabase  int,          -- len(df_sb) carregado do banco antes do sync
    tickets_build     int,          -- len(df_build) usado para gerar o HTML
    tickets_upsertados int,         -- tickets enviados ao Supabase neste run
    csat_bruto        int,          -- len(all_ratings) antes do filtro de período
    csat_periodo      int,          -- len(ratings) após filtro
    csat_deduplicados int,          -- len(df_csat) após drop_duplicates por ticket_id
    duracao_s         int,
    erros             text[]        DEFAULT '{}'
);

-- Apenas leitura para anon; escrita via service_role (pipeline)
ALTER TABLE public.cx_sync_audit ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'cx_sync_audit' AND policyname = 'anon_read'
  ) THEN
    CREATE POLICY anon_read ON public.cx_sync_audit
      FOR SELECT TO anon USING (true);
  END IF;
END $$;
