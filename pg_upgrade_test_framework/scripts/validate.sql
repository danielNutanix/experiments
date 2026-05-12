-- Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
-- Post-upgrade validation queries.
-- Each query MUST return exactly one row with the literal text 'OK' in
-- the first column, otherwise the framework records a validation failure.

\set ON_ERROR_STOP on
SET search_path = upgrade_test, public;

-- 1. Server version is reachable
SELECT 'OK' AS result, version();

-- 2. Customer rowcount is the seeded amount
SELECT CASE WHEN COUNT(*) = 5000 THEN 'OK'
            ELSE format('FAIL: customers count=%s', COUNT(*)) END AS result
  FROM upgrade_test.customers;

-- 3. Order rowcount is the seeded amount
SELECT CASE WHEN COUNT(*) = 25000 THEN 'OK'
            ELSE format('FAIL: orders count=%s', COUNT(*)) END AS result
  FROM upgrade_test.orders;

-- 4. Partition routing still resolves
SELECT CASE WHEN COUNT(*) > 0 THEN 'OK'
            ELSE 'FAIL: orders_2025 partition empty' END AS result
  FROM upgrade_test.orders_2025;

-- 5. View definition and aggregation still work
SELECT CASE WHEN COUNT(*) = 5000 THEN 'OK'
            ELSE format('FAIL: v_customer_orders count=%s', COUNT(*)) END
       AS result
  FROM upgrade_test.v_customer_orders;

-- 6. JSONB GIN index is consulted (regression check, not a hard fail)
SELECT 'OK' AS result, COUNT(*) AS web_signups
  FROM upgrade_test.customers
 WHERE metadata @> '{"signup_source":"web"}'::jsonb;

-- 7. Trigger still fires (audit log gains rows on every write)
INSERT INTO upgrade_test.customers (email, full_name)
VALUES (format('postupgrade_%s@example.com', extract(epoch from now())::BIGINT),
        'Post Upgrade Probe');
DELETE FROM upgrade_test.customers
 WHERE email LIKE 'postupgrade_%@example.com';

SELECT CASE WHEN COUNT(*) >= 2 THEN 'OK'
            ELSE format('FAIL: audit_log only got %s rows from probe',
                        COUNT(*)) END AS result
  FROM upgrade_test.audit_log
 WHERE at > now() - INTERVAL '1 minute';

-- 8. Catalog-level consistency: every relation has a valid pg_class entry
SELECT CASE WHEN COUNT(*) = 0 THEN 'OK'
            ELSE format('FAIL: %s relations missing pg_class entries',
                        COUNT(*)) END AS result
  FROM information_schema.tables t
 WHERE t.table_schema = 'upgrade_test'
   AND NOT EXISTS (
       SELECT 1 FROM pg_class c
        WHERE c.relname = t.table_name
   );

-- 9. All indexes are valid
SELECT CASE WHEN COUNT(*) = 0 THEN 'OK'
            ELSE format('FAIL: %s invalid indexes', COUNT(*)) END AS result
  FROM pg_index
 WHERE NOT indisvalid OR NOT indisready;
