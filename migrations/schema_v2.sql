-- Schema v2 migration: add user-editable merchant_display + notes columns.
-- Run automatically by init_db() when existing schema_version = 1.

ALTER TABLE transactions ADD COLUMN merchant_display TEXT;
ALTER TABLE transactions ADD COLUMN notes TEXT;

-- Backfill: existing rows get merchant_display = merchant_raw
UPDATE transactions
   SET merchant_display = merchant_raw
 WHERE merchant_display IS NULL;

-- Bump schema_version
UPDATE meta SET value = '2' WHERE key = 'schema_version';
