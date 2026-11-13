-- Several Schwab accounts: each transaction belongs to one. Rows stored before this belong to
-- the account the app used then, keyed "live".
ALTER TABLE schwab_transactions ADD COLUMN account text NOT NULL DEFAULT 'live';
ALTER TABLE schwab_transactions DROP CONSTRAINT schwab_transactions_pkey;
ALTER TABLE schwab_transactions ADD PRIMARY KEY (account, activity_id);
CREATE INDEX schwab_transactions_account_time ON schwab_transactions (account, time);
