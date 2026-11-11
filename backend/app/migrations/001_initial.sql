CREATE TABLE settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notifications_sent (
    key text PRIMARY KEY,
    sent_at timestamptz NOT NULL DEFAULT now()
);

-- One 30-day constant-maturity at-the-money IV per underlying per trading day, in percent.
CREATE TABLE iv_history (
    symbol text NOT NULL,
    day date NOT NULL,
    iv30 double precision NOT NULL,
    source text NOT NULL,
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (symbol, day)
);

CREATE TABLE betas (
    symbol text PRIMARY KEY,
    beta double precision NOT NULL,
    as_of date NOT NULL
);
