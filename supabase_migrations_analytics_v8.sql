-- mb_tipo_cliente_mensal: breakdown por tipo de cliente por mês (first_order_date)
CREATE TABLE IF NOT EXISTS mb_tipo_cliente_mensal (
    periodo         DATE        NOT NULL,
    tipo_cliente    TEXT        NOT NULL,
    qtd_usuarios    INT         DEFAULT 0,
    pct_do_total    NUMERIC(8,4),
    media_pedidos   NUMERIC(8,2),
    receita_total   NUMERIC(15,2),
    ticket_medio    NUMERIC(10,2),
    data_carga      DATE,
    PRIMARY KEY (periodo, tipo_cliente)
);

CREATE INDEX IF NOT EXISTS idx_tipo_cliente_mensal_periodo ON mb_tipo_cliente_mensal(periodo);

ALTER TABLE mb_tipo_cliente_mensal ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read_tipo_cliente_mensal"
    ON mb_tipo_cliente_mensal FOR SELECT
    TO anon USING (true);

CREATE POLICY "service_write_tipo_cliente_mensal"
    ON mb_tipo_cliente_mensal FOR ALL
    TO service_role USING (true) WITH CHECK (true);
