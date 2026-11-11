-- Rows imported from the trading-history spreadsheet, keyed by their opening details so a
-- re-upload updates rows instead of duplicating them.
CREATE TABLE sheet_rows (
    row_key text PRIMARY KEY,
    account text NOT NULL,
    data jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- Daily closing prices for the dashboard's analysis (VIX at entry, underlying moves).
CREATE TABLE daily_prices (
    symbol text NOT NULL,
    day date NOT NULL,
    close double precision NOT NULL,
    PRIMARY KEY (symbol, day)
);
