-- ============================================================
-- Migração: Base de Conhecimento (GitHub → Zendesk → Supabase)
-- Execute no SQL Editor do painel Supabase
-- ============================================================

-- Artigos sincronizados (um registro por arquivo .md do GitHub)
CREATE TABLE IF NOT EXISTS kb_articles (
    id                  BIGSERIAL PRIMARY KEY,
    github_path         TEXT        NOT NULL UNIQUE,   -- ex: "knowledge-base/onboarding/intro.md"
    title               TEXT        NOT NULL,
    html_body           TEXT,
    zendesk_article_id  BIGINT,                        -- ID retornado pela API do Zendesk
    status              TEXT        CHECK (status IN ('criado','atualizado','erro')),
    error_msg           TEXT,
    last_synced_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Log de cada execução do sync
CREATE TABLE IF NOT EXISTS kb_sync_log (
    id           BIGSERIAL    PRIMARY KEY,
    synced_at    TIMESTAMPTZ  DEFAULT NOW(),
    total_files  INT,
    criados      INT,
    atualizados  INT,
    erros        INT,
    duracao_s    NUMERIC(8,1)
);

-- Índices úteis
CREATE INDEX IF NOT EXISTS idx_kb_articles_status      ON kb_articles (status);
CREATE INDEX IF NOT EXISTS idx_kb_articles_synced_at   ON kb_articles (last_synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_sync_log_synced_at   ON kb_sync_log (synced_at DESC);
