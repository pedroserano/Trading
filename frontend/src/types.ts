export interface Flag {
  kind: "profit" | "dte" | "tested" | "loss" | "data";
  label: string;
  detail: string;
}

export interface LegView {
  symbol: string;
  kind: "OPTION" | "EQUITY";
  put_call: "P" | "C" | null;
  strike: number | null;
  expiration: string | null;
  dte: number | null;
  quantity: number;
  avg_price: number;
  mark: number | null;
  delta: number | null;
  theta: number | null;
  iv: number | null;
}

export interface StrategyNumbers {
  open_value: number;
  credit: boolean;
  value: number | null;
  pl: number | null;
  pct_max_profit: number | null;
  max_profit: number | null;
  max_loss: number | null;
  dte: number | null;
  expiration: string | null;
  contracts: number;
  delta: number | null;
  theta: number | null;
  vega: number | null;
  short_deltas: (number | null)[];
  short_strikes: number[];
}

export interface StrategyView {
  name: string;
  defined_risk: boolean;
  numbers: StrategyNumbers;
  flags: Flag[];
  legs: LegView[];
}

export interface IvRank {
  /** Null while fewer than 20 days of history exist. */
  rank: number | null;
  percentile: number;
  days: number;
  low: number;
  high: number;
  source: "VIX" | "recorded";
}

export interface UnderlyingView {
  symbol: string;
  price: number | null;
  change_pct: number | null;
  beta: number | null;
  ivr: IvRank | null;
  pl: number | null;
  delta: number | null;
  delta_bw: number | null;
  theta: number | null;
  strategies: StrategyView[];
}

export interface Snapshot {
  pending?: boolean;
  generated_at: number;
  mode: "demo" | "live";
  connection: {
    connected: boolean;
    expires_in_days: number | null;
    detail: string | null;
    visible_accounts: number | null;
  };
  needs: { what: "connect" | "select_account"; message: string } | null;
  error: string | null;
  session: "open" | "closed";
  data: { quotes_at: number | null; positions_at: number | null };
  market: { vix: number | null; spy: number | null; spy_change_pct: number | null };
  account: {
    last4: string;
    type: string | null;
    net_liq: number | null;
    cash: number | null;
    buying_power: number | null;
    maintenance: number | null;
    bp_used_pct: number | null;
    bp_ceiling_pct: number | null;
  } | null;
  underlyings: UnderlyingView[];
  totals: {
    pl: number | null;
    theta: number | null;
    theta_pct_nl: number | null;
    delta_bw: number | null;
    flags: number;
  };
  ivr: { SPY: IvRank | null };
}
