export type ServiceState =
  | "ONLINE"
  | "CONNECTED"
  | "DEGRADED"
  | "DISCONNECTED"
  | "STARTING"
  | "RECONNECTING"
  | "STOPPED"
  | "DISABLED"
  | "PLANNED"
  | "ERROR"
  | "UNKNOWN";

export interface AccountSnapshot {
  timestamp: string;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  margin_level: number;
  profit: number;
  leverage: number;
  currency: string;
  server: string;
  trade_mode: number;
}

export interface SymbolSnapshot {
  timestamp: string;
  name: string;
  bid: number;
  ask: number;
  spread: number;
  digits: number;
  point: number;
  trade_mode: string | null;
  session: string | null;
  market_status: string;
}

export interface Position {
  position_id: string;
  broker_ticket: number;
  direction: string;
  open_time: string;
  volume: number;
  open_price: number;
  current_price: number;
  stop_loss: number;
  take_profit: number;
  profit: number;
  swap: number;
  observed_at?: string | null;
  snapshot_id?: string | null;
  freshness?: "LIVE" | "STALE" | "STATE_SYNC_PENDING" | "UNKNOWN";
}

export interface PositionStatus {
  open_positions: number;
  observed_at: string | null;
  snapshot_id: string | null;
  freshness: "LIVE" | "STALE" | "STATE_SYNC_PENDING" | "UNKNOWN";
  read_only: boolean;
}

export interface Trade {
  trade_id: string;
  broker_ticket: number | null;
  direction: string;
  entry_time: string | null;
  entry_price: number | null;
  exit_time: string | null;
  exit_price: number | null;
  volume: number | null;
  initial_stop_loss: number | null;
  initial_take_profit: number | null;
  final_stop_loss: number | null;
  final_take_profit: number | null;
  risk_percent: number | null;
  planned_rr: number | null;
  net_profit: number | null;
  realized_r: number | null;
  duration_seconds: number | null;
  mae: number | null;
  mfe: number | null;
  exit_reason: string | null;
  session: string | null;
  market_regime: string | null;
  strategy_version: string | null;
  prompt_version: string | null;
  model_version: string | null;
  ai_decision_id: string | null;
}

export interface TradeEvent {
  event_id: string;
  timestamp: string;
  event_type: string;
  price: number | null;
  volume: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  pnl: number | null;
  reason: string | null;
  source: string;
}

export interface AiDecision {
  decision_id: string;
  timestamp: string;
  action: "BUY" | "SELL" | "WAIT" | "HOLD" | "MODIFY" | "CLOSE";
  confidence: number | null;
  structured_rationale: Record<string, unknown>;
  requested_risk_percent: number | null;
  validation_status: string | null;
  execution_status: string | null;
  model_name: string | null;
  model_version: string | null;
  prompt_version: string | null;
  result_trade_id: string | null;
}

export interface ShadowDecision {
  decision_id: string;
  created_at: string;
  market_snapshot_id: string | null;
  symbol: string;
  m5_candle_timestamp: string;
  decision: "BUY" | "SELL" | "NO_TRADE";
  market_regime: string;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  risk_reward_ratio: number | null;
  requested_risk_percent: number | null;
  approved_risk_percent: number | null;
  hypothetical_volume: number | null;
  confidence: number | null;
  strategy_name: string;
  strategy_version: string;
  reason_codes: string[];
  human_readable_reason: string;
  risk_gate_state: string;
  data_freshness: string;
  execution_allowed: false;
  outcome_status: string;
}

export interface RiskSnapshot {
  timestamp: string;
  equity: number;
  balance: number;
  open_risk_percent: number | null;
  remaining_risk_percent: number | null;
  drawdown_percent: number | null;
  max_trade_risk_percent: number;
  max_aggregate_risk_percent: number;
  shadow_engine_enabled: boolean;
  shadow_decision_timeframe: string;
  shadow_min_rr: number;
  shadow_notify_signals: boolean;
  shadow_notify_no_trade: boolean;
  open_positions_count: number;
  unbounded_positions_count: number;
  daily_pnl: number | null;
  daily_realized_loss: number | null;
  margin_usage_percent: number | null;
  risk_per_position: Array<{
    ticket: number;
    risk_percent: number | null;
    risk_amount: number | null;
    bounded_by_stop: boolean;
  }>;
  observed_at?: string | null;
  snapshot_id?: string | null;
  freshness?: "LIVE" | "STALE" | "STATE_SYNC_PENDING" | "UNKNOWN";
  read_only?: boolean;
}

export interface SystemEvent {
  event_id: string;
  event_type: string;
  timestamp: string;
  source: string;
  severity: "DEBUG" | "INFO" | "SUCCESS" | "WARNING" | "ERROR" | "CRITICAL";
  correlation_id: string | null;
  payload: Record<string, unknown>;
}

export interface HealthResponse {
  database: ServiceState;
  services: Record<string, ServiceState>;
  components: Array<{
    component: string;
    status: string;
    timestamp: string;
    latency_ms: number | null;
    message: string | null;
  }>;
  error_count: number;
  checked_at: string;
  started_at: string;
  uptime_seconds: number;
  last_market_update: string | null;
  websocket_clients: number;
  database_identity?: string;
}

export interface LiveStatusResponse {
  state: string;
  symbol: string | null;
  updated_at: string | null;
  freshness: Record<string, { state: string; observed_at: string | null; age_seconds: number | null }>;
  workers: Array<{
    name: string;
    state: string;
    last_success_at: string | null;
    last_failed_at?: string | null;
    failure_count?: number;
    message: string | null;
  }>;
  read_only: boolean;
}

export interface ShadowHealthResponse {
  state: ServiceState;
  observed_at: string | null;
  age_seconds: number | null;
  last_success_at: string | null;
  last_failed_at: string | null;
  queue_depth: number;
  queue_capacity?: number;
  deferred_count?: number;
  catchup_pending_count?: number;
  total_backlog?: number;
  processing_lag_seconds?: number | null;
  latest_available_m5?: string | null;
  latest_received_m5?: string | null;
  latest_processed_m5?: string | null;
  latest_decision_m5?: string | null;
  last_failure?: string | null;
  error_category: string | null;
  latest_persisted_m5?: string | null;
  latest_completed_snapshot?: {
    id: string;
    timestamp: string;
    latest_m5: string | null;
  } | null;
  last_received_candle_at?: string | null;
  last_processed_candle_at?: string | null;
  last_decision_at?: string | null;
  execution_allowed: false;
  read_only: boolean;
}

export interface ShadowOutcome {
  outcome_id: string;
  decision_id: string;
  symbol: string;
  strategy_version: string;
  evaluation_policy_version: string;
  decision_m5_timestamp: string;
  side: "BUY" | "SELL";
  terminal_status: "PENDING" | "TP_HIT" | "SL_HIT" | "AMBIGUOUS" | "EXPIRED" | "INVALID";
  realized_r: number | null;
  bars_held: number;
  mfe_r: number | null;
  mae_r: number | null;
  reason_code: string;
  execution_allowed: false;
}

export interface ShadowOutcomeHealth {
  state: ServiceState;
  observed_at: string | null;
  age_seconds: number | null;
  evaluation_policy_version: string;
  execution_allowed: false;
}

export interface ShadowPerformance {
  eligible_trades: number;
  resolved_sample_size: number;
  pending: number;
  tp_hits: number;
  sl_hits: number;
  ambiguous: number;
  expired: number;
  invalid: number;
  no_trade_rate: number | null;
  win_rate: number | null;
  average_r: number | null;
  total_r: number | null;
  expectancy_r: number | null;
  profit_factor: number | null;
  max_drawdown_r: number | null;
  execution_allowed: false;
}

export interface SupervisorResponse {
  components: Record<string, {
    pid: number | null;
    state: string;
    started_at: string | null;
    last_heartbeat: string | null;
    exit_code: number | null;
    restart_count: number;
  }>;
  read_only: boolean;
  execution: string;
}

export interface PerformancePoint {
  trade_id: string;
  timestamp: string;
  cumulative_net_profit?: number;
  drawdown?: number;
}

export interface AccountCurvePoint {
  snapshot_id: string;
  timestamp: string;
  equity: number;
  balance: number;
  drawdown_percent: number | null;
}

export interface PublicConfig {
  telegram_enabled: boolean;
  telegram_control_enabled: boolean;
  max_trade_risk_percent: number;
  max_aggregate_risk_percent: number;
  shadow_strategy?: string;
  read_only: boolean;
  timezone: string;
}

export interface ResearchDataset {
  dataset_id: string;
  symbol: string;
  timeframe: string;
  source: string;
  start_at: string;
  end_at: string;
  row_count: number;
  created_at: string;
  dataset_hash: string;
  timezone: string;
  closed_candles_only: boolean;
  metadata: Record<string, unknown>;
}

export interface ResearchRun {
  run_id: string;
  run_name?: string | null;
  strategy_id: string;
  strategy_version: string;
  status: string;
  dataset_id: string;
  dataset_hash?: string;
  parameters: Record<string, unknown>;
  summary: { buy?: number; sell?: number; no_trade?: number; [key: string]: unknown };
  execution_allowed: false;
}

export interface ResearchCurve {
  run_id: string;
  equity: Array<{ timestamp: string; value: number }>;
  drawdown: Array<{ timestamp: string; value: number }>;
  execution_allowed: false;
  point_count?: number;
  display_point_count?: number;
  display_sampled?: boolean;
  display_limit?: number;
}

export interface ResearchRobustness {
  robustness_id: string;
  strategy_id: string;
  strategy_version: string;
  config_hash: string;
  dataset_id: string;
  dataset_hash: string;
  source_run_id: string | null;
  status: string;
  seed: number;
  simulation_count: number;
  summary: Record<string, unknown>;
  execution_allowed: false;
}

export interface ForwardSession {
  session_id: string;
  strategy_id: string;
  strategy_version: string;
  strategy_config_hash: string;
  started_at: string;
  source_identity: string;
  symbol: string;
  timeframes: string[];
  rr: number;
  cost_policy: Record<string, unknown>;
  status: string;
  error_reason: string | null;
  execution_allowed: false;
  updated_at: string;
  read_only: true;
}

export interface ForwardHealth {
  worker: string;
  state: string;
  observed_at: string | null;
  age_seconds: number | null;
  heartbeat_at?: string | null;
  session_id?: string | null;
  last_closed_m5?: string | null;
  last_closed_m15?: string | null;
  last_closed_h1?: string | null;
  last_zone_created?: string | null;
  last_signal?: string | null;
  open_shadow_trades?: number;
  total_forward_signals?: number;
  failure_count?: number;
  execution_allowed: false;
  read_only: true;
}

export interface ForwardSignal {
  signal_id: string;
  session_id: string;
  timestamp: string;
  decision: "BUY" | "SELL";
  zone_id: string;
  entry: number;
  stop: number;
  risk_distance: number;
  rr: number;
  take_profit: number;
  pair_first_timestamp: string | null;
  pair_second_timestamp: string | null;
  h1_context: Record<string, unknown>;
  confirmation_candle: Record<string, unknown>;
  market_observation: Record<string, unknown>;
  strategy_hash: string;
  execution_allowed: false;
  read_only: true;
}

export interface ForwardTrade {
  trade_id: string;
  session_id: string;
  signal_id: string;
  timestamp: string;
  side: "BUY" | "SELL";
  state: "OPEN" | "TP" | "SL" | "AMBIGUOUS" | "EXPIRED";
  entry: number;
  stop: number;
  take_profit: number;
  risk_distance: number;
  terminal_timestamp: string | null;
  mark_price: number | null;
  gross_r: number | null;
  net_r: number | null;
  bars_held: number;
  minutes_held: number | null;
  mfe_price: number | null;
  mae_price: number | null;
  mfe_r: number | null;
  mae_r: number | null;
  spread_points: number;
  spread_observation: string;
  entry_slippage_points: number;
  exit_slippage_points: number;
  commission_r: number;
  total_cost_r: number | null;
  reason_code: string | null;
  execution_allowed: false;
  read_only: true;
}

export interface ForwardPerformanceBlock {
  signals: number;
  resolved: number;
  gross_total_r: number;
  net_total_r: number;
  gross_expectancy: number | null;
  net_expectancy: number | null;
  profit_factor: number | null;
  win_rate: number | null;
  max_drawdown_r: number;
  max_loss_streak: number;
  max_win_streak: number;
  average_spread: number | null;
  average_cost_r: number | null;
  TP: number;
  SL: number;
  AMBIGUOUS: number;
  EXPIRED: number;
  OPEN: number;
}

export interface ForwardPerformance {
  session: ForwardSession | null;
  session_id?: string;
  strategy_id?: string;
  strategy_version?: string;
  strategy_config_hash?: string;
  started_at?: string;
  status?: string;
  signals: number;
  BUY: number;
  SELL: number;
  OPEN: number;
  TP: number;
  SL: number;
  AMBIGUOUS: number;
  EXPIRED: number;
  combined: ForwardPerformanceBlock;
  BUY_metrics: ForwardPerformanceBlock;
  SELL_metrics: ForwardPerformanceBlock;
  tp_sl_only: ForwardPerformanceBlock;
  expired_only: ForwardPerformanceBlock;
  execution_allowed: false;
  read_only: true;
}
