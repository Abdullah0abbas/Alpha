-- Migration 015: Add deposit_tx_hash and payout_tx_hash columns with unique constraints to deals table
ALTER TABLE deals ADD COLUMN IF NOT EXISTS deposit_tx_hash VARCHAR(128);
ALTER TABLE deals ADD COLUMN IF NOT EXISTS payout_tx_hash VARCHAR(128);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_deposit_tx_hash
    ON deals (deposit_tx_hash)
    WHERE deposit_tx_hash IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_payout_tx_hash
    ON deals (payout_tx_hash)
    WHERE payout_tx_hash IS NOT NULL;
