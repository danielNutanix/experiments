-- Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
-- Representative seed dataset for PG upgrade compatibility testing.
-- Designed to exercise: heap tables, btree indexes, hash indexes, partitioning,
-- foreign keys, constraints, triggers, JSONB, arrays, sequences, views.

CREATE SCHEMA IF NOT EXISTS upgrade_test;
SET search_path = upgrade_test, public;

-- Plain heap + btree
CREATE TABLE IF NOT EXISTS customers (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    full_name   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    tags        TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]
);
CREATE INDEX IF NOT EXISTS idx_customers_created_at ON customers (created_at);
CREATE INDEX IF NOT EXISTS idx_customers_metadata_gin ON customers USING gin (metadata);

-- Range-partitioned table to validate partitioning across PG versions
CREATE TABLE IF NOT EXISTS orders (
    id          BIGSERIAL,
    customer_id BIGINT NOT NULL REFERENCES customers (id),
    placed_at   TIMESTAMPTZ NOT NULL,
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    status      TEXT NOT NULL DEFAULT 'NEW',
    PRIMARY KEY (id, placed_at)
) PARTITION BY RANGE (placed_at);

CREATE TABLE IF NOT EXISTS orders_2024 PARTITION OF orders
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
CREATE TABLE IF NOT EXISTS orders_2025 PARTITION OF orders
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE IF NOT EXISTS orders_default PARTITION OF orders DEFAULT;

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id);

-- Trigger + sequence
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    table_name  TEXT NOT NULL,
    op          TEXT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION trg_audit_customers() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO upgrade_test.audit_log (table_name, op)
    VALUES (TG_TABLE_NAME, TG_OP);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_customers ON customers;
CREATE TRIGGER audit_customers
    AFTER INSERT OR UPDATE OR DELETE ON customers
    FOR EACH ROW EXECUTE FUNCTION trg_audit_customers();

-- View to confirm view definitions survive the upgrade
CREATE OR REPLACE VIEW v_customer_orders AS
    SELECT c.id AS customer_id, c.email, COUNT(o.id) AS order_count,
           COALESCE(SUM(o.total_cents), 0) AS total_spent_cents
      FROM customers c
      LEFT JOIN orders o ON o.customer_id = c.id
     GROUP BY c.id, c.email;

-- Seed ~5,000 customers and ~25,000 orders idempotently
INSERT INTO customers (email, full_name, metadata, tags)
SELECT format('user_%s@example.com', g),
       format('Test User %s', g),
       jsonb_build_object('signup_source', CASE WHEN g % 3 = 0 THEN 'web'
                                                WHEN g % 3 = 1 THEN 'mobile'
                                                ELSE 'partner' END,
                          'segment', (g % 10)),
       ARRAY['seeded', format('cohort_%s', g % 20)]
  FROM generate_series(1, 5000) g
ON CONFLICT (email) DO NOTHING;

INSERT INTO orders (customer_id, placed_at, total_cents, status)
SELECT (random() * 4999 + 1)::BIGINT,
       (TIMESTAMPTZ '2024-01-01' + (random() * INTERVAL '700 days')),
       (random() * 50000 + 100)::INTEGER,
       (ARRAY['NEW','PAID','SHIPPED','DELIVERED','CANCELLED'])
           [1 + (random() * 4)::INT]
  FROM generate_series(1, 25000)
  WHERE NOT EXISTS (SELECT 1 FROM orders LIMIT 1);

ANALYZE upgrade_test.customers;
ANALYZE upgrade_test.orders;
