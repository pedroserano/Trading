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
  trade_id: number | string | null;
  rolls: number;
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
  mode: "demo" | "live" | "sim";
  connection: {
    connected: boolean;
    expires_in_days: number | null;
    detail: string | null;
    visible_accounts: number | null;
  };
  needs: { what: "connect" | "select_account"; message: string } | null;
  error: string | null;
  session: "open" | "closed";
  data: {
    quotes_at: number | null;
    positions_at: number | null;
    source?: "stream" | "poll";
    stream_detail?: string | null;
  };
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

export interface WatchRow {
  symbol: string;
  price: number | null;
  change_pct: number | null;
  iv30: number | null;
  ivr: IvRank | null;
  expected_move: number | null;
  expiration: string | null;
  dte: number | null;
  spread_pct: number | null;
  atm_oi: number | null;
  error: string | null;
  held: boolean;
  position_delta: number | null;
  position_theta: number | null;
}

export interface Expiration {
  date: string;
  dte: number;
  monthly: boolean;
}

export interface Contract {
  symbol: string;
  bid: number | null;
  ask: number | null;
  mark: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  iv: number | null;
  oi: number | null;
  volume: number | null;
}

export interface ChainRow {
  strike: number;
  call: Contract | null;
  put: Contract | null;
}

export interface Chain {
  symbol: string;
  expiration: string;
  underlying_price: number | null;
  dte: number | null;
  atm_iv: number | null;
  expected_move: number | null;
  ivr: IvRank | null;
  rows: ChainRow[];
}

export interface BuildLeg {
  symbol: string;
  put_call: "P" | "C";
  strike: number;
  expiration: string;
  quantity: number;
  price: number;
  iv: number | null;
  delta: number | null;
  theta: number | null;
  vega: number | null;
}

export interface Analysis {
  net: number;
  credit: boolean;
  price_per_unit: number | null;
  units: number;
  max_profit: number | null;
  max_loss: number | null;
  unlimited_profit: boolean;
  unlimited_loss: boolean;
  breakevens: number[];
  pop: number;
  dte: number;
  vol: number;
  expected_move: number;
  delta: number | null;
  theta: number | null;
  vega: number | null;
  bp_estimate: number;
  bp_exact: boolean;
  beta: number | null;
  curve: { x: number[]; expiry: number[]; today: number[] };
  portfolio_after: {
    delta_bw: number | null;
    theta: number | null;
    bp_used_pct: number | null;
    bp_ceiling_pct: number | null;
  };
}

export interface OrderLeg {
  symbol: string;
  quantity: number;
  effect: "open" | "close";
}

export interface Preview {
  account: "sim" | "live";
  intent: string;
  strategy: string;
  underlying: string;
  spot: number | null;
  units: number;
  prices: { natural: number | null; mid: number | null; far: number | null };
  limit_net: number | null;
  duration: "DAY" | "GTC";
  legs: {
    symbol: string;
    instruction: string;
    quantity: number;
    bid: number | null;
    ask: number | null;
    mark: number | null;
    put_call: string;
    strike: number;
    expiration: string | null;
  }[];
  bp_effect: number | null;
  defined_risk: boolean;
  bp_used_after: number | null;
  commission: number;
  errors: string[];
  warnings: string[];
  schwab_json: unknown;
  schwab_preview: {
    ok: boolean;
    simulated?: boolean;
    error?: string;
    fields: Record<string, number>;
    messages: { level: string; text: string }[];
  } | null;
}

export interface SimOrder {
  id: number;
  created_at: number;
  status: "WORKING" | "FILLED" | "CANCELED" | "EXPIRED" | "REJECTED" | "REPLACED";
  intent: string;
  trade_id: number | null;
  underlying: string;
  strategy: string;
  legs: OrderLeg[];
  units: number;
  limit_net: number;
  duration: "DAY" | "GTC";
  fill_net: number | null;
  filled_at: number | null;
  reason: string | null;
  schwab_json: unknown;
}

export interface TradeEvent {
  at: number;
  kind: "open" | "close" | "roll" | "adjust" | "expire" | "settle" | "assign" | "exercise" | "carried";
  order_id: number | string | null;
  group?: string | null;
  note?: string | null;
  auto_linked?: boolean;
  net: number;
  commission: number;
  legs: { symbol: string; quantity: number; price: number }[];
}

export interface SimTrade {
  id: number | string;
  carried_in?: boolean;
  holdings?: Record<string, number>;
  underlying: string;
  strategy: string;
  opened_at: number;
  closed_at: number | null;
  status: "open" | "closed";
  events: TradeEvent[];
  realized_pl: number | null;
}

export interface StatRow {
  strategy: string;
  trades: number;
  win_rate: number;
  total_pl: number;
  avg_pl: number;
  avg_days: number;
  pl_per_day: number;
}

export interface Proposal {
  group: string;
  chain: string;
  reason: string;
}

export interface Mismatch {
  symbol: string;
  chains: number;
  schwab: number;
}

export interface Activity {
  account: "live" | "sim";
  cash?: number;
  start_cash?: number;
  net_liq?: number | null;
  proposals?: Proposal[];
  mismatches?: Mismatch[];
  synced_at?: number | null;
  error?: string | null;
  orders: SimOrder[];
  trades: SimTrade[];
  stats: StatRow[];
}

export type AutoLevel = "off" | "notify" | "propose" | "auto";

export interface AutoProposal {
  id: string;
  trade_id: string;
  rule: string;
  underlying: string;
  strategy: string;
  reason: string;
  created_at: number;
  due_at: number | null;
  bodies: { intent: string; legs: { symbol: string; quantity: number; effect: string }[] }[];
}

export interface AutoLogEntry {
  at: number;
  trade_id: string | number | null;
  rule: string | null;
  text: string;
  order_id?: number;
}

export interface AutomationView {
  paused: boolean;
  settings: { levels: Record<string, AutoLevel>; window_start: string; window_end: string; daily_cap: number };
  trades: Record<string, { enabled: boolean }>;
  proposals: AutoProposal[];
  log: AutoLogEntry[];
  orders_today: number;
  in_window: boolean;
}
