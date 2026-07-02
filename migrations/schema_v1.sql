-- ============================================================================
-- finance-tracker schema v1
-- ============================================================================

-- meta: schema version + small key/value config
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- transactions: main table for credit card transactions
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Source tracking (for dedup across re-runs)
    source_email_id TEXT UNIQUE,            -- Gmail Message-ID header
    source_received_at TIMESTAMP,           -- when email landed in inbox

    -- Transaction timing
    transaction_date DATE,                  -- when the transaction occurred
    posted_date DATE,                       -- when posted to account (NULL if pending)
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'posted' | 'reversed'

    -- Amounts (multi-currency support)
    amount_original REAL NOT NULL,
    currency_original TEXT NOT NULL,        -- ISO 4217 (CAD/USD/SGD/...)
    amount_base REAL NOT NULL,
    currency_base TEXT NOT NULL,            -- base currency at time of import
    fx_rate REAL,                           -- NULL when same currency
    was_foreign INTEGER NOT NULL DEFAULT 0, -- 1 if currency_original != card home

    -- Card / merchant
    card_nickname TEXT NOT NULL,            -- e.g., 'scotiabank visa'
    merchant_raw TEXT,                      -- raw text from email
    merchant_normalized TEXT,               -- cleaned: 'STARBUCKS #1234' → 'starbucks'

    -- Classification
    category TEXT,                          -- one of the 10 categories
    tags TEXT,                              -- JSON array, e.g. '["coffee"]'
    classification_source TEXT,             -- 'cache'|'preset'|'llm'|'llm-web'|'manual'
    classification_confidence REAL,         -- 0.0 to 1.0

    -- Notion sync state
    notion_page_id TEXT,
    last_notion_sync TIMESTAMP,

    -- Audit
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    user_modified_fields TEXT               -- JSON array of fields user manually edited
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_tx_merchant_normalized ON transactions(merchant_normalized);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_notion_page_id ON transactions(notion_page_id);

-- merchant_rules: cached merchant→category/tags mapping (self-learning)
CREATE TABLE IF NOT EXISTS merchant_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_normalized TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    tags TEXT,                              -- JSON array
    source TEXT NOT NULL,                   -- 'preset'|'llm-confirmed'|'manual'
    confirmation_count INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- sync_runs: audit log of import/sync operations
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    trigger TEXT NOT NULL,                  -- 'manual'|'scheduled'|'shortcut'
    emails_processed INTEGER DEFAULT 0,
    transactions_created INTEGER DEFAULT 0,
    transactions_updated INTEGER DEFAULT 0,
    notion_synced INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    error_log TEXT,                         -- JSON array of error strings
    status TEXT                             -- 'success'|'partial'|'failure'
);

-- Triggers: auto-update updated_at on row modification
CREATE TRIGGER IF NOT EXISTS trg_transactions_updated_at
AFTER UPDATE ON transactions
FOR EACH ROW
BEGIN
    UPDATE transactions SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_merchant_rules_updated_at
AFTER UPDATE ON merchant_rules
FOR EACH ROW
BEGIN
    UPDATE merchant_rules SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_meta_updated_at
AFTER UPDATE ON meta
FOR EACH ROW
BEGIN
    UPDATE meta SET updated_at = CURRENT_TIMESTAMP WHERE key = OLD.key;
END;

-- Initial meta values
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta (key, value) VALUES ('base_currency', 'CAD');
