-- Raw Schwab transactions, kept as received so trade chains can be rebuilt at any time.
CREATE TABLE schwab_transactions (
    activity_id text PRIMARY KEY,
    time timestamptz NOT NULL,
    raw jsonb NOT NULL
);
CREATE INDEX schwab_transactions_time ON schwab_transactions (time);
