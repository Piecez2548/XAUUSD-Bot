"""Local-first FastAPI read API for the Trading Observatory."""
# ruff: noqa: E501

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Select, desc, func, or_, select

from analytics.service import AnalyticsService, TradeSample
from api.realtime import RealtimeHub
from config.settings import Settings, load_settings
from persistence.database import Database
from persistence.orm import (
    AccountRecord,
    AccountSnapshotRecord,
    AiDecisionRecord,
    BrokerDealRecord,
    CandleRecord,
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    MarketSnapshotRecord,
    PositionRecord,
    PositionSnapshotRecord,
    ResearchDatasetRecord,
    ResearchDecisionRecord,
    ResearchMetricRecord,
    ResearchOutcomeRecord,
    ResearchRobustnessRecord,
    ResearchRunRecord,
    RiskSnapshotRecord,
    ShadowDecisionRecord,
    ShadowOutcomeRecord,
    StrategyActivationRecord,
    SymbolRecord,
    SystemEventRecord,
    SystemHealthRecord,
    TradeEventRecord,
    TradeRecord,
)
from persistence.repositories import SystemHealthRepository
from services.forward_shadow import (
    forward_health,
    forward_performance,
    forward_signals,
    forward_trades,
)
from services.shadow_outcome import (
    OUTCOME_POLICY_VERSION,
    performance_breakdown,
)
from services.shadow_outcome import (
    performance_summary as shadow_performance_summary,
)
from services.strategy_platform import StrategyRegistry
from services.supervisor import ProcessRecord, record_process_is_alive
from services.worker_health import derive_worker_state, worker_health_payload, worker_health_ttl

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _supervisor_health_state(project_root: Path, now: datetime, ttl_seconds: float) -> str:
    """Derive safe supervisor health from its persisted heartbeat registry."""

    registry_path = project_root / "data" / "process_registry.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return "UNKNOWN"
    if not isinstance(payload, dict) or not payload:
        return "UNKNOWN"
    heartbeats: list[datetime] = []
    states: list[str] = []
    runtime_states: list[str] = []
    verified_runtime: dict[str, bool] = {}
    for component, raw_record in payload.items():
        if not isinstance(raw_record, dict):
            continue
        try:
            record = ProcessRecord(
                **{
                    "component": component,
                    "pid": None,
                    "started_at": None,
                    "exit_code": None,
                    **raw_record,
                    "command": tuple(raw_record.get("command") or ()),
                    "process_tree": tuple(raw_record.get("process_tree") or ()),
                }
            )
        except (TypeError, ValueError):
            continue
        state = str(record.state).upper()
        states.append(state)
        if component in {"api", "live"}:
            verified = record_process_is_alive(record)
            verified_runtime[component] = verified
            runtime_states.append("CONNECTED" if verified else "STOPPED")
        raw_heartbeat = record.last_heartbeat
        if isinstance(raw_heartbeat, str):
            try:
                heartbeat = datetime.fromisoformat(raw_heartbeat)
            except ValueError:
                continue
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
            heartbeats.append(heartbeat.astimezone(UTC))
    if (
        not states
        or not heartbeats
        or not runtime_states
        or {
            component.casefold()
            for component in payload
            if component.casefold() in {"api", "live"}
        }
        != {"api", "live"}
    ):
        return "UNKNOWN"
    if any(
        not verified_runtime.get(component, False)
        and str(payload[component].get("state", "")).upper() in {"RUNNING", "STARTING"}
        for component in ("api", "live")
        if isinstance(payload.get(component), dict)
    ):
        return "DEGRADED"
    if not any(state == "CONNECTED" for state in runtime_states):
        return "STOPPED" if all(state == "STOPPED" for state in states) else "DEGRADED"
    if any(state == "STOPPED" for state in runtime_states):
        return "DEGRADED"
    if any(state in {"ERROR", "CRASHED", "DEGRADED", "STOPPED"} for state in states):
        return "DEGRADED"
    latest = max(heartbeats)
    if (now - latest).total_seconds() > max(30.0, ttl_seconds):
        return "DEGRADED"
    return "CONNECTED" if all(state in {"RUNNING", "STARTING"} for state in states) else "UNKNOWN"


def _display_downsample(
    points: list[dict[str, Any]],
    limit: int,
    *,
    value_key: str = "value",
) -> list[dict[str, Any]]:
    """Deterministically bound visualization points without changing persisted evidence."""

    if len(points) <= limit:
        return points
    anchors = {0, len(points) - 1}
    indexes = set(anchors)
    numeric = [
        (index, point.get(value_key))
        for index, point in enumerate(points)
        if isinstance(point.get(value_key), (int, float))
    ]
    extrema = []
    if numeric:
        extrema = [min(numeric, key=lambda item: item[1])[0], max(numeric, key=lambda item: item[1])[0]]
    for index in extrema:
        if len(indexes) >= limit:
            break
        indexes.add(index)
    remaining = max(0, limit - len(indexes))
    if remaining:
        step = (len(points) - 1) / (remaining + 1)
        indexes.update(round(step * (position + 1)) for position in range(remaining))
    indexes.update(anchors)
    return [points[index] for index in sorted(indexes)[:limit]]


def _trade_dict(row: TradeRecord) -> dict[str, Any]:
    return {
        "trade_id": row.id,
        "broker_ticket": row.broker_ticket,
        "direction": row.direction,
        "entry_time": row.entry_time,
        "entry_price": row.entry_price,
        "exit_time": row.exit_time,
        "exit_price": row.exit_price,
        "volume": row.volume,
        "initial_stop_loss": row.initial_stop_loss,
        "initial_take_profit": row.initial_take_profit,
        "final_stop_loss": row.final_stop_loss,
        "final_take_profit": row.final_take_profit,
        "risk_percent": row.risk_percent,
        "planned_rr": row.planned_rr,
        "realized_r": row.realized_r,
        "net_profit": row.net_profit,
        "duration_seconds": row.duration_seconds,
        "mae": row.mae,
        "mfe": row.mfe,
        "exit_reason": row.exit_reason,
        "session": row.session,
        "market_regime": row.market_regime,
        "strategy_version": row.strategy_version,
        "prompt_version": row.prompt_version,
        "model_version": row.model_version,
        "ai_decision_id": row.ai_decision_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _snapshot_freshness(
    risk: RiskSnapshotRecord | None,
    market: MarketSnapshotRecord | None,
    *,
    max_age_seconds: float,
    active_count: int | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime | None]:
    observed = risk.timestamp if risk is not None else market.timestamp if market else None
    if observed is None:
        return "UNKNOWN", None
    coherent = bool(
        risk is not None
        and market is not None
        and risk.market_snapshot_id
        and risk.market_snapshot_id == market.id
        and (active_count is None or risk.open_positions_count == active_count)
    )
    if not coherent:
        return "STATE_SYNC_PENDING", observed
    current = now or datetime.now(UTC)
    observed_utc = observed if observed.tzinfo else observed.replace(tzinfo=UTC)
    age = (current - observed_utc).total_seconds()
    return ("STALE" if age > max_age_seconds else "LIVE"), observed


def _latest_complete_snapshot(
    session: Any,
) -> tuple[RiskSnapshotRecord | None, MarketSnapshotRecord | None]:
    candidates = session.execute(
        select(RiskSnapshotRecord, MarketSnapshotRecord)
        .join(
            MarketSnapshotRecord,
            RiskSnapshotRecord.market_snapshot_id == MarketSnapshotRecord.id,
        )
        .where(MarketSnapshotRecord.positions_observed_successfully.is_(True))
        .order_by(desc(RiskSnapshotRecord.timestamp))
    ).all()
    latest_risk_any = session.scalar(
        select(RiskSnapshotRecord).order_by(desc(RiskSnapshotRecord.timestamp)).limit(1)
    )
    latest_market_any = session.scalar(
        select(MarketSnapshotRecord).order_by(desc(MarketSnapshotRecord.timestamp)).limit(1)
    )
    for risk, market in candidates:
        observed_count = (
            session.scalar(
                select(func.count())
                .select_from(PositionSnapshotRecord)
                .where(PositionSnapshotRecord.market_snapshot_id == market.id)
            )
            or 0
        )
        if risk.open_positions_count == market.open_position_count == observed_count:
            if (latest_risk_any is not None and latest_risk_any.timestamp > risk.timestamp) or (
                latest_market_any is not None and latest_market_any.timestamp > market.timestamp
            ):
                continue
            return risk, market
    return None, None


def _trade_samples(rows: Sequence[TradeRecord]) -> list[TradeSample]:
    return [
        TradeSample(
            trade_id=row.id,
            direction=row.direction,
            exit_time=row.exit_time,
            net_profit=row.net_profit,
            realized_r=row.realized_r,
            duration_seconds=row.duration_seconds,
            mae=row.mae,
            mfe=row.mfe,
            session=row.session,
            market_regime=row.market_regime,
            strategy_version=row.strategy_version,
            prompt_version=row.prompt_version,
            model_version=row.model_version,
            confidence=None,
        )
        for row in rows
        if row.exit_time is not None and row.net_profit is not None
    ]


def _service_state_from_events(
    rows: Sequence[SystemEventRecord],
    *,
    connected_type: str,
    disconnected_types: tuple[str, ...],
    now: datetime,
) -> str:
    relevant = [
        row
        for row in rows
        if row.event_type == connected_type or row.event_type in disconnected_types
    ]
    if not relevant:
        return "UNKNOWN"
    latest = max(relevant, key=lambda row: (row.timestamp, row.event_id))
    if latest.event_type in disconnected_types:
        return "DISCONNECTED"
    if now - latest.timestamp > timedelta(minutes=5):
        return "UNKNOWN"
    return "CONNECTED"


def _csv_response(filename: str, rows: list[dict[str, Any]]) -> StreamingResponse:
    buffer = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in row.items()
                }
            )
    response = StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
) -> FastAPI:
    runtime_settings = settings or load_settings()
    started_at = datetime.now(UTC)
    db = database or Database(runtime_settings.database_url, project_root=PROJECT_ROOT)
    db.create_schema()
    realtime = RealtimeHub(
        db, history_interval_seconds=runtime_settings.live_history_interval_seconds
    )
    analytics = AnalyticsService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await realtime.start()
        yield
        await realtime.stop()
        if database is None:
            db.dispose()

    app = FastAPI(
        title="XAUUSD AI Trader Observatory API",
        version="1.6.0",
        description="Read-only observability and analytics API. No trade execution endpoints.",
        lifespan=lifespan,
    )
    app.state.database = db
    app.state.settings = runtime_settings
    app.state.realtime = realtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
    )

    def completed_trades() -> list[TradeRecord]:
        with db.session() as session:
            return list(
                session.scalars(
                    select(TradeRecord)
                    .where(TradeRecord.exit_time.is_not(None), TradeRecord.net_profit.is_not(None))
                    .order_by(TradeRecord.exit_time)
                )
            )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        now = datetime.now(UTC)
        supervisor_state = _supervisor_health_state(
            PROJECT_ROOT,
            now,
            max(30.0, runtime_settings.live_account_interval_seconds * 4),
        )
        database_state = db.healthcheck()
        return {
            "status": "healthy" if database_state else "unhealthy",
            "database": "connected" if database_state else "unavailable",
            "supervisor": supervisor_state,
            "timestamp": now,
            "read_only": True,
            "version": "1.6.0",
            "database_identity": db.database_identity,
        }

    @app.get("/api/research/datasets")
    def research_datasets(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        with db.session() as session:
            rows = session.scalars(select(ResearchDatasetRecord).order_by(desc(ResearchDatasetRecord.created_at)).limit(limit)).all()
            return [
                {"dataset_id": row.dataset_id, "symbol": row.symbol, "timeframe": row.timeframe,
                 "source": row.source, "start_at": row.start_at, "end_at": row.end_at,
                 "row_count": row.row_count, "created_at": row.created_at, "dataset_hash": row.dataset_hash,
                 "timezone": row.timezone, "closed_candles_only": row.closed_candles_only,
                 "metadata": row.metadata_json}
                for row in rows
            ]

    def _research_run_dict(row: ResearchRunRecord, session: Any | None = None) -> dict[str, Any]:
        dataset_label = row.dataset_id
        if session is not None:
            dataset_label = session.scalar(
                select(ResearchDatasetRecord.dataset_id).where(ResearchDatasetRecord.id == row.dataset_id)
            ) or dataset_label
        return {"run_id": row.run_id, "run_name": row.run_name, "strategy_id": row.strategy_id,
                "strategy_version": row.strategy_version, "config_hash": row.config_hash,
                "dataset_id": dataset_label, "dataset_hash": row.dataset_hash, "symbol": row.symbol,
                "timeframe": row.timeframe, "status": row.status, "started_at": row.started_at,
                "completed_at": row.completed_at, "created_at": row.created_at,
                "git_commit": row.git_commit, "git_dirty": row.git_dirty, "engine_version": row.engine_version,
                "parameters": row.parameters_json, "split_definition": row.split_definition,
                "summary": row.summary_json, "execution_allowed": False}

    @app.get("/api/research/runs")
    def research_runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        with db.session() as session:
            return [_research_run_dict(row, session) for row in session.scalars(select(ResearchRunRecord).order_by(desc(ResearchRunRecord.created_at)).limit(limit)).all()]

    def _research_run_or_404(session, run_id: str) -> ResearchRunRecord:
        row = session.scalar(select(ResearchRunRecord).where(ResearchRunRecord.run_id == run_id))
        if row is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return row

    @app.get("/api/research/runs/{run_id}")
    def research_run(run_id: str) -> dict[str, Any]:
        with db.session() as session:
            return _research_run_dict(_research_run_or_404(session, run_id), session)

    @app.get("/api/research/runs/{run_id}/metrics")
    def research_metrics(run_id: str) -> list[dict[str, Any]]:
        with db.session() as session:
            run = _research_run_or_404(session, run_id)
            rows = session.scalars(select(ResearchMetricRecord).where(ResearchMetricRecord.run_id == run.id).order_by(ResearchMetricRecord.metric_key)).all()
            return [{"metric_key": row.metric_key, "value": row.value, "denominator": row.denominator, "dimension": row.dimension_json} for row in rows]

    @app.get("/api/research/runs/{run_id}/trades")
    def research_trades(run_id: str, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> list[dict[str, Any]]:
        with db.session() as session:
            run = _research_run_or_404(session, run_id)
            rows = session.scalars(select(ResearchDecisionRecord).where(ResearchDecisionRecord.run_id == run.id, ResearchDecisionRecord.decision.in_(("BUY", "SELL"))).order_by(ResearchDecisionRecord.timestamp).offset(offset).limit(limit)).all()
            return [{"decision_id": row.id, "timestamp": row.timestamp, "decision": row.decision, "reason_code": row.reason_code,
                     "entry_price": row.entry_price, "stop_loss": row.stop_loss, "take_profit": row.take_profit,
                     "risk_reward_ratio": row.risk_reward_ratio, "execution_allowed": False} for row in rows]

    @app.get("/api/research/runs/{run_id}/funnel")
    def research_funnel(run_id: str) -> dict[str, Any]:
        with db.session() as session:
            run = _research_run_or_404(session, run_id)
            summary = run.summary_json if isinstance(run.summary_json, dict) else {}
            funnel = summary.get("funnel") if isinstance(summary.get("funnel"), dict) else {}
            return {
                "run_id": run.run_id,
                "snapshots": int(funnel.get("snapshots", summary.get("eligible_candles", 0))),
                "decisions": int(funnel.get("decisions", summary.get("buy", 0) + summary.get("sell", 0))),
                "eligible": int(funnel.get("eligible", summary.get("buy", 0) + summary.get("sell", 0))),
                "no_trade_aggregate": int(funnel.get("no_trade_aggregate", summary.get("no_trade", 0))),
                "reason_counts": summary.get("reason_counts", {}),
                "execution_allowed": False,
            }

    @app.get("/api/research/compare")
    def research_compare(
        run_id: Annotated[list[str] | None, Query()] = None,
        strategy_id: Annotated[list[str] | None, Query()] = None,
        rr: Annotated[float | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        with db.session() as session:
            query = select(ResearchRunRecord).order_by(desc(ResearchRunRecord.created_at))
            if run_id:
                query = query.where(ResearchRunRecord.run_id.in_(run_id))
            rows = list(session.scalars(query.limit(200)).all())
            if strategy_id:
                selected: list[ResearchRunRecord] = []
                aliases = {
                    "trend_pullback_v1": {"trend_pullback_v1", "trend_pullback"},
                    "pair_zone_v1": {"pair_zone_v1"},
                }
                for requested in strategy_id:
                    accepted_ids = aliases.get(requested, {requested})
                    match = next((row for row in rows
                                  if row.status == "COMPLETED"
                                  and row.strategy_id in accepted_ids
                                  and "chronological" not in (row.run_name or "")
                                  and not (row.run_name or "").endswith(("-development", "-validation", "-holdout"))
                                  and (rr is None or row.parameters_json.get("rr") == rr)), None)
                    if match is not None:
                        selected.append(match)
                rows = selected
            return [_research_run_dict(row, session) for row in rows]

    @app.get("/api/research/runs/{run_id}/curve")
    def research_curve(
        run_id: str,
        display_limit: int = Query(default=2_000, ge=2, le=5_000),
    ) -> dict[str, Any]:
        with db.session() as session:
            run = _research_run_or_404(session, run_id)
            rows = session.execute(
                select(ResearchDecisionRecord.timestamp, ResearchOutcomeRecord.realized_r)
                .join(ResearchOutcomeRecord, ResearchOutcomeRecord.decision_id == ResearchDecisionRecord.id)
                .where(ResearchDecisionRecord.run_id == run.id)
                .order_by(ResearchDecisionRecord.timestamp)
            ).all()
            equity: list[dict[str, Any]] = []
            drawdown: list[dict[str, Any]] = []
            cumulative = 0.0
            peak = 0.0
            for timestamp, realized_r in rows:
                if realized_r is None:
                    continue
                cumulative += float(realized_r)
                peak = max(peak, cumulative)
                equity.append({"timestamp": timestamp, "value": cumulative})
                drawdown.append({"timestamp": timestamp, "value": peak - cumulative})
            display_equity = _display_downsample(equity, display_limit)
            display_drawdown = _display_downsample(drawdown, display_limit)
            return {
                "run_id": run.run_id,
                "equity": display_equity,
                "drawdown": display_drawdown,
                "point_count": len(equity),
                "display_point_count": len(display_equity),
                "display_sampled": len(display_equity) < len(equity),
                "display_limit": display_limit,
                "execution_allowed": False,
            }

    @app.get("/api/research/robustness")
    def research_robustness(
        strategy_id: str = Query(default="pair_zone_v1"),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        """Expose persisted read-only robustness evidence from one source of truth."""
        with db.session() as session:
            rows = session.scalars(
                select(ResearchRobustnessRecord)
                .where(ResearchRobustnessRecord.strategy_id == strategy_id)
                .order_by(desc(ResearchRobustnessRecord.created_at))
                .limit(limit)
            ).all()
            return [{
                "robustness_id": row.robustness_id,
                "strategy_id": row.strategy_id,
                "strategy_version": row.strategy_version,
                "config_hash": row.config_hash,
                "dataset_id": session.scalar(
                    select(ResearchDatasetRecord.dataset_id).where(ResearchDatasetRecord.id == row.dataset_id)
                ) or row.dataset_id,
                "dataset_hash": row.dataset_hash,
                "source_run_id": row.source_run_id,
                "status": row.status,
                "seed": row.seed,
                "simulation_count": row.simulation_count,
                "created_at": row.created_at,
                "summary": row.summary_json,
                "execution_allowed": False,
            } for row in rows]

    @app.get("/api/account")
    def account() -> dict[str, Any] | None:
        with db.session() as session:
            row = session.execute(
                select(AccountSnapshotRecord, AccountRecord)
                .join(AccountRecord, AccountRecord.id == AccountSnapshotRecord.account_id)
                .order_by(desc(AccountSnapshotRecord.timestamp))
                .limit(1)
            ).first()
            if row is None:
                return None
            snapshot, record = row
            return {
                "timestamp": snapshot.timestamp,
                "balance": snapshot.balance,
                "equity": snapshot.equity,
                "margin": snapshot.margin,
                "free_margin": snapshot.free_margin,
                "margin_level": snapshot.margin_level,
                "profit": snapshot.profit,
                "leverage": snapshot.leverage,
                "currency": record.currency,
                "server": record.server,
                "trade_mode": record.trade_mode,
            }

    @app.get("/api/symbol")
    def symbol() -> dict[str, Any] | None:
        with db.session() as session:
            row = session.execute(
                select(MarketSnapshotRecord, SymbolRecord)
                .join(SymbolRecord, SymbolRecord.id == MarketSnapshotRecord.symbol_id)
                .order_by(desc(MarketSnapshotRecord.timestamp))
                .limit(1)
            ).first()
            if row is None:
                return None
            snapshot, record = row
            return {
                "timestamp": snapshot.timestamp,
                "name": record.name,
                "bid": snapshot.bid,
                "ask": snapshot.ask,
                "spread": snapshot.spread,
                "digits": record.digits,
                "point": record.point,
                "trade_tick_size": record.trade_tick_size,
                "trade_tick_value": record.trade_tick_value,
                "contract_size": record.contract_size,
                "trade_mode": record.trade_mode_name,
                "session": snapshot.session,
                "market_status": "UNKNOWN",
            }

    @app.get("/api/positions")
    def positions() -> list[dict[str, Any]]:
        with db.session() as session:
            risk_row, market_row = _latest_complete_snapshot(session)
            if risk_row is None or market_row is None:
                risk_row = session.scalar(
                    select(RiskSnapshotRecord).order_by(desc(RiskSnapshotRecord.timestamp)).limit(1)
                )
                market_row = session.scalar(
                    select(MarketSnapshotRecord)
                    .order_by(desc(MarketSnapshotRecord.timestamp))
                    .limit(1)
                )
            coherent_snapshot_id = (
                market_row.id
                if risk_row is not None
                and market_row is not None
                and risk_row.market_snapshot_id == market_row.id
                else None
            )
            if coherent_snapshot_id:
                records = session.scalars(
                    select(PositionRecord)
                    .join(
                        PositionSnapshotRecord,
                        PositionSnapshotRecord.position_id == PositionRecord.id,
                    )
                    .where(PositionSnapshotRecord.market_snapshot_id == coherent_snapshot_id)
                    .order_by(PositionRecord.first_seen_at)
                ).all()
            else:
                records = session.scalars(
                    select(PositionRecord)
                    .where(PositionRecord.closed_observed_at.is_(None))
                    .order_by(PositionRecord.first_seen_at)
                ).all()
            freshness, observed_at = _snapshot_freshness(
                risk_row,
                market_row,
                max_age_seconds=runtime_settings.data_stale_position_seconds,
                active_count=len(records),
            )
            result = []
            for record in records:
                position_query = select(PositionSnapshotRecord).where(
                    PositionSnapshotRecord.position_id == record.id
                )
                if coherent_snapshot_id:
                    position_query = position_query.where(
                        PositionSnapshotRecord.market_snapshot_id == coherent_snapshot_id
                    )
                latest = session.scalar(
                    position_query.order_by(desc(PositionSnapshotRecord.timestamp)).limit(1)
                )
                if latest is not None:
                    result.append(
                        {
                            "position_id": record.id,
                            "broker_ticket": record.broker_ticket,
                            "direction": record.direction,
                            "open_time": record.broker_open_time,
                            "last_seen_at": record.last_seen_at,
                            "volume": latest.volume,
                            "open_price": latest.open_price,
                            "current_price": latest.current_price,
                            "stop_loss": latest.stop_loss,
                            "take_profit": latest.take_profit,
                            "profit": latest.profit,
                            "swap": latest.swap,
                            "observed_at": observed_at,
                            "snapshot_id": latest.market_snapshot_id,
                            "freshness": freshness,
                        }
                    )
            return result

    @app.get("/api/positions/status")
    def positions_status() -> dict[str, Any]:
        with db.session() as session:
            risk_row, market_row = _latest_complete_snapshot(session)
            if risk_row is None or market_row is None:
                risk_row = session.scalar(
                    select(RiskSnapshotRecord).order_by(desc(RiskSnapshotRecord.timestamp)).limit(1)
                )
                market_row = session.scalar(
                    select(MarketSnapshotRecord)
                    .order_by(desc(MarketSnapshotRecord.timestamp))
                    .limit(1)
                )
            active_count = (
                session.scalar(
                    select(func.count())
                    .select_from(PositionRecord)
                    .where(PositionRecord.closed_observed_at.is_(None))
                )
                or 0
            )
            freshness, observed_at = _snapshot_freshness(
                risk_row,
                market_row,
                max_age_seconds=runtime_settings.data_stale_position_seconds,
                active_count=active_count,
            )
            return {
                "open_positions": active_count,
                "observed_at": observed_at,
                "snapshot_id": risk_row.market_snapshot_id if risk_row else None,
                "freshness": freshness,
                "read_only": True,
            }

    @app.get("/api/history/deals")
    def history_deals(
        symbol_name: str | None = Query(default=None, alias="symbol"),
        limit: int = Query(default=200, ge=1, le=1_000),
    ) -> list[dict[str, Any]]:
        query = select(BrokerDealRecord)
        if symbol_name:
            query = query.where(BrokerDealRecord.symbol == symbol_name)
        with db.session() as session:
            rows = session.scalars(query.order_by(desc(BrokerDealRecord.timestamp)).limit(limit))
            return [
                {
                    "deal_ticket": row.deal_ticket,
                    "order_ticket": row.order_ticket,
                    "position_id": row.position_id,
                    "symbol": row.symbol,
                    "timestamp": row.timestamp,
                    "deal_type": row.deal_type,
                    "entry_type": row.entry_type,
                    "volume": row.volume,
                    "price": row.price,
                    "profit": row.profit,
                    "commission": row.commission,
                    "swap": row.swap,
                    "fee": row.fee,
                    "reason": row.reason,
                }
                for row in rows
            ]

    @app.get("/api/trades")
    def trades(
        direction: Literal["BUY", "SELL"] | None = None,
        result: Literal["WIN", "LOSS", "BE"] | None = None,
        session_name: str | None = Query(default=None, alias="session"),
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        min_risk: float | None = Query(default=None, ge=0),
        max_risk: float | None = Query(default=None, ge=0),
        model_version: str | None = None,
        prompt_version: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        query: Select[tuple[TradeRecord]] = select(TradeRecord)
        if direction:
            query = query.where(TradeRecord.direction == direction)
        if result == "WIN":
            query = query.where(TradeRecord.net_profit > 0)
        elif result == "LOSS":
            query = query.where(TradeRecord.net_profit < 0)
        elif result == "BE":
            query = query.where(TradeRecord.net_profit == 0)
        if session_name:
            query = query.where(TradeRecord.session == session_name)
        if date_from:
            query = query.where(TradeRecord.entry_time >= date_from)
        if date_to:
            query = query.where(TradeRecord.entry_time <= date_to)
        if min_risk is not None:
            query = query.where(TradeRecord.risk_percent >= min_risk)
        if max_risk is not None:
            query = query.where(TradeRecord.risk_percent <= max_risk)
        if model_version:
            query = query.where(TradeRecord.model_version == model_version)
        if prompt_version:
            query = query.where(TradeRecord.prompt_version == prompt_version)
        query = query.order_by(desc(TradeRecord.entry_time)).offset(offset).limit(limit)
        with db.session() as session:
            return [_trade_dict(row) for row in session.scalars(query)]

    @app.get("/api/trades/{trade_id}")
    def trade_detail(trade_id: str) -> dict[str, Any]:
        with db.session() as session:
            row = session.get(TradeRecord, trade_id)
            if row is None:
                raise HTTPException(status_code=404, detail="trade not found")
            return _trade_dict(row)

    @app.get("/api/trades/{trade_id}/events")
    def trade_events(trade_id: str) -> list[dict[str, Any]]:
        with db.session() as session:
            if session.get(TradeRecord, trade_id) is None:
                raise HTTPException(status_code=404, detail="trade not found")
            rows = session.scalars(
                select(TradeEventRecord)
                .where(TradeEventRecord.trade_id == trade_id)
                .order_by(TradeEventRecord.timestamp)
            )
            return [
                {
                    "event_id": row.id,
                    "timestamp": row.timestamp,
                    "event_type": row.event_type,
                    "price": row.price,
                    "volume": row.volume,
                    "stop_loss": row.stop_loss,
                    "take_profit": row.take_profit,
                    "pnl": row.pnl,
                    "reason": row.reason,
                    "source": row.source,
                }
                for row in rows
            ]

    @app.get("/api/decisions")
    def decisions(
        action: Literal["BUY", "SELL", "WAIT", "HOLD", "MODIFY", "CLOSE"] | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        query: Select[tuple[AiDecisionRecord]] = select(AiDecisionRecord)
        if action:
            query = query.where(AiDecisionRecord.action == action)
        with db.session() as session:
            rows = session.scalars(query.order_by(desc(AiDecisionRecord.timestamp)).limit(limit))
            return [
                {
                    "decision_id": row.id,
                    "timestamp": row.timestamp,
                    "action": row.action,
                    "confidence": row.confidence,
                    "structured_rationale": row.structured_rationale,
                    "requested_risk_percent": row.requested_risk_percent,
                    "validation_status": row.validation_status,
                    "execution_status": row.execution_status,
                    "model_name": row.model_name,
                    "model_version": row.model_version,
                    "prompt_version": row.prompt_version,
                    "result_trade_id": row.result_trade_id,
                }
                for row in rows
            ]

    def _shadow_decision_dict(row: ShadowDecisionRecord) -> dict[str, Any]:
        return {
            "decision_id": row.id,
            "created_at": row.created_at,
            "market_snapshot_id": row.market_snapshot_id,
            "symbol": row.symbol,
            "m5_candle_timestamp": row.m5_candle_timestamp,
            "decision": row.decision,
            "market_regime": row.market_regime,
            "entry_price": row.entry_price,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "risk_reward_ratio": row.risk_reward_ratio,
            "requested_risk_percent": row.requested_risk_percent,
            "approved_risk_percent": row.approved_risk_percent,
            "hypothetical_volume": row.hypothetical_volume,
            "confidence": row.confidence,
            "strategy_name": row.strategy_name,
            "strategy_version": row.strategy_version,
            "reason_codes": row.reason_codes,
            "human_readable_reason": row.human_readable_reason,
            "risk_gate_state": row.risk_gate_state,
            "data_freshness": row.data_freshness,
            "execution_allowed": False,
            "outcome_status": row.outcome_status,
        }

    def _shadow_outcome_dict(row: ShadowOutcomeRecord) -> dict[str, Any]:
        return {
            "outcome_id": row.id,
            "decision_id": row.decision_id,
            "symbol": row.symbol,
            "strategy_version": row.strategy_version,
            "evaluation_policy_version": row.evaluation_policy_version,
            "decision_m5_timestamp": row.decision_m5_timestamp,
            "side": row.side,
            "entry_price": row.entry_price,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "initial_risk_distance": row.initial_risk_distance,
            "target_r_multiple": row.target_r_multiple,
            "evaluation_started_at": row.evaluation_started_at,
            "terminal_candle_timestamp": row.terminal_candle_timestamp,
            "terminal_status": row.terminal_status,
            "exit_price": row.exit_price,
            "realized_r": row.realized_r,
            "bars_held": row.bars_held,
            "max_favorable_excursion_price": row.max_favorable_excursion_price,
            "max_adverse_excursion_price": row.max_adverse_excursion_price,
            "mfe_r": row.mfe_r,
            "mae_r": row.mae_r,
            "evaluated_at": row.evaluated_at,
            "reason_code": row.reason_code,
            "read_only": True,
            "execution_allowed": False,
        }

    @app.get("/api/shadow/decision")
    def latest_shadow_decision() -> dict[str, Any] | None:
        with db.session() as session:
            row = session.scalar(
                select(ShadowDecisionRecord)
                .order_by(
                    desc(ShadowDecisionRecord.m5_candle_timestamp),
                    desc(ShadowDecisionRecord.created_at),
                    desc(ShadowDecisionRecord.id),
                )
                .limit(1)
            )
            return _shadow_decision_dict(row) if row else None

    @app.get("/api/shadow/decisions")
    def shadow_decisions(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        with db.session() as session:
            rows = session.scalars(
                select(ShadowDecisionRecord)
                .order_by(
                    desc(ShadowDecisionRecord.m5_candle_timestamp),
                    desc(ShadowDecisionRecord.created_at),
                    desc(ShadowDecisionRecord.id),
                )
                .limit(limit)
            )
            return [_shadow_decision_dict(row) for row in rows]

    @app.get("/api/shadow/summary")
    def shadow_summary() -> dict[str, int]:
        with db.session() as session:
            rows = session.execute(
                select(ShadowDecisionRecord.decision, func.count()).group_by(
                    ShadowDecisionRecord.decision
                )
            ).all()
            result = {"total": 0, "BUY": 0, "SELL": 0, "NO_TRADE": 0, "pending_outcomes": 0}
            result.update({decision: int(count) for decision, count in rows})
            result["total"] = result["BUY"] + result["SELL"] + result["NO_TRADE"]
            result["pending_outcomes"] = int(
                session.scalar(
                    select(func.count())
                    .select_from(ShadowDecisionRecord)
                    .where(ShadowDecisionRecord.outcome_status == "PENDING")
                )
                or 0
            )
            return result

    @app.get("/api/shadow/health")
    def shadow_health() -> dict[str, Any]:
        """Expose safe shadow-worker liveness and candle progress."""

        now = datetime.now(UTC)
        with db.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:shadow")
                .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
                .limit(1)
            )
            latest_m5 = session.scalar(
                select(CandleRecord.timestamp)
                .where(CandleRecord.timeframe == "M5")
                .order_by(desc(CandleRecord.timestamp), desc(CandleRecord.id))
                .limit(1)
            )
            latest_snapshot = session.scalar(
                select(MarketSnapshotRecord)
                .order_by(desc(MarketSnapshotRecord.timestamp), desc(MarketSnapshotRecord.id))
                .limit(1)
            )
        payload = worker_health_payload(
            row,
            interval_seconds=max(15.0, runtime_settings.live_account_interval_seconds * 4),
            now=now,
        )
        payload.update(
            {
                "latest_persisted_m5": latest_m5,
                "latest_completed_snapshot": (
                    {
                        "id": latest_snapshot.id,
                        "timestamp": latest_snapshot.timestamp,
                        "latest_m5": (latest_snapshot.latest_candles or {})
                        .get("M5", {})
                        .get("timestamp"),
                    }
                    if latest_snapshot
                    else None
                ),
                "checked_at": now,
                "read_only": True,
            }
        )
        return payload

    @app.get("/api/shadow/outcomes")
    def shadow_outcomes(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        with db.session() as session:
            rows = session.scalars(
                select(ShadowOutcomeRecord)
                .where(ShadowOutcomeRecord.evaluation_policy_version == OUTCOME_POLICY_VERSION)
                .order_by(
                    desc(ShadowOutcomeRecord.decision_m5_timestamp),
                    desc(ShadowOutcomeRecord.created_at),
                )
                .limit(limit)
            )
            return [_shadow_outcome_dict(row) for row in rows]

    @app.get("/api/shadow/outcome/{decision_id}")
    def shadow_outcome(decision_id: str) -> dict[str, Any]:
        with db.session() as session:
            row = session.scalar(
                select(ShadowOutcomeRecord).where(
                    ShadowOutcomeRecord.decision_id == decision_id,
                    ShadowOutcomeRecord.evaluation_policy_version == OUTCOME_POLICY_VERSION,
                )
            )
            if row is None:
                raise HTTPException(status_code=404, detail="shadow outcome not found")
            return _shadow_outcome_dict(row)

    @app.get("/api/shadow/performance")
    def shadow_performance() -> dict[str, Any]:
        return shadow_performance_summary(db, policy_version=OUTCOME_POLICY_VERSION)

    @app.get("/api/shadow/performance/breakdown")
    def shadow_performance_breakdown() -> list[dict[str, Any]]:
        return performance_breakdown(db, policy_version=OUTCOME_POLICY_VERSION)

    @app.get("/api/shadow/outcome-health")
    def shadow_outcome_health() -> dict[str, Any]:
        now = datetime.now(UTC)
        with db.session() as session:
            row = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:shadow_outcome")
                .order_by(desc(SystemHealthRecord.timestamp), desc(SystemHealthRecord.id))
                .limit(1)
            )
        payload = worker_health_payload(
            row,
            interval_seconds=max(15.0, runtime_settings.live_candle_interval_seconds * 4),
            now=now,
        )
        payload.update(
            {
                "evaluation_policy_version": OUTCOME_POLICY_VERSION,
                "read_only": True,
                "execution_allowed": False,
            }
        )
        return payload

    def _forward_session_dict(row: ForwardValidationSessionRecord | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "session_id": row.session_id, "strategy_id": row.strategy_id,
            "strategy_version": row.strategy_version, "strategy_config_hash": row.strategy_config_hash,
            "started_at": row.started_at, "source_identity": row.source_identity,
            "symbol": row.symbol, "timeframes": row.timeframes_json, "rr": row.rr,
            "cost_policy": row.cost_policy_json, "status": row.status,
            "error_reason": row.error_reason, "execution_allowed": False, "updated_at": row.updated_at,
            "read_only": True,
        }

    def _forward_signal_dict(row: ForwardSignalRecord) -> dict[str, Any]:
        return {
            "signal_id": row.signal_id, "session_id": row.session_id, "timestamp": row.timestamp,
            "decision": row.decision, "zone_id": row.zone_id, "entry": row.entry_price,
            "stop": row.stop_loss, "risk_distance": row.risk_distance, "rr": row.rr,
            "take_profit": row.take_profit, "pair_first_timestamp": row.pair_first_timestamp,
            "pair_second_timestamp": row.pair_second_timestamp, "h1_context": row.h1_context_json,
            "confirmation_candle": row.confirmation_candle_json,
            "market_observation": row.market_observation_json, "strategy_hash": row.strategy_hash,
            "execution_allowed": False, "read_only": True,
        }

    def _forward_trade_dict(row: ForwardTradeRecord) -> dict[str, Any]:
        return {
            "trade_id": row.trade_id, "session_id": row.session_id, "signal_id": row.signal_id,
            "timestamp": row.timestamp, "side": row.side, "state": row.state,
            "entry": row.entry_price, "stop": row.stop_loss, "take_profit": row.take_profit,
            "risk_distance": row.risk_distance, "terminal_timestamp": row.terminal_timestamp,
            "mark_price": row.mark_price, "gross_r": row.gross_r, "net_r": row.net_r,
            "bars_held": row.bars_held, "minutes_held": row.minutes_held,
            "mfe_price": row.mfe_price, "mae_price": row.mae_price, "mfe_r": row.mfe_r,
            "mae_r": row.mae_r, "spread_points": row.spread_points,
            "spread_observation": row.spread_observation,
            "entry_slippage_points": row.entry_slippage_points,
            "exit_slippage_points": row.exit_slippage_points, "commission_r": row.commission_r,
            "total_cost_r": row.total_cost_r, "reason_code": row.reason_code,
            "execution_allowed": False, "read_only": True,
        }

    @app.get("/api/forward/session")
    def forward_session() -> dict[str, Any] | None:
        from services.forward_shadow import latest_forward_session
        return _forward_session_dict(latest_forward_session(db))

    @app.get("/api/forward/health")
    def forward_worker_health() -> dict[str, Any]:
        return forward_health(db, runtime_settings)

    @app.get("/api/forward/signals")
    def forward_signal_list(limit: int = Query(default=100, ge=1, le=500), session_id: str | None = None) -> list[dict[str, Any]]:
        return [_forward_signal_dict(row) for row in forward_signals(db, session_id=session_id, limit=limit)]

    @app.get("/api/forward/trades")
    def forward_trade_list(limit: int = Query(default=100, ge=1, le=500), session_id: str | None = None) -> list[dict[str, Any]]:
        return [_forward_trade_dict(row) for row in forward_trades(db, session_id=session_id, limit=limit)]

    @app.get("/api/forward/performance")
    def forward_performance_summary(session_id: str | None = None) -> dict[str, Any]:
        return forward_performance(db, session_id=session_id)

    @app.get("/api/research/strategies")
    def research_strategies() -> dict[str, Any]:
        registry = StrategyRegistry(runtime_settings)
        with db.session() as session:
            activation = session.scalar(
                select(StrategyActivationRecord)
                .order_by(StrategyActivationRecord.effective_from_m5.desc(),
                          StrategyActivationRecord.requested_at.desc())
                .limit(1)
            )
        return {
            "active_strategy": runtime_settings.shadow_strategy,
            "available_strategies": registry.identifiers(),
            "activation": {
                "strategy_id": activation.strategy_id,
                "strategy_version": activation.strategy_version,
                "config_version": activation.config_version,
                "config_hash": activation.config_hash,
                "effective_from_m5": activation.effective_from_m5,
                "execution_allowed": False,
            } if activation else None,
            "read_only": True,
            "execution_allowed": False,
        }

    @app.get("/api/decisions/{decision_id}")
    def decision_detail(decision_id: str) -> dict[str, Any]:
        with db.session() as session:
            row = session.get(AiDecisionRecord, decision_id)
            if row is None:
                raise HTTPException(status_code=404, detail="decision not found")
            return {
                column.name: getattr(row, column.name)
                for column in AiDecisionRecord.__table__.columns
            }

    @app.get("/api/performance/summary")
    def performance_summary() -> dict[str, Any]:
        return analytics.summary(_trade_samples(completed_trades()))

    @app.get("/api/performance/equity")
    def performance_equity() -> list[dict[str, Any]]:
        return analytics.equity_curve(_trade_samples(completed_trades()))

    @app.get("/api/performance/account-curve")
    def performance_account_curve(
        display_limit: int | None = Query(default=None, ge=2, le=50_000),
    ) -> list[dict[str, Any]]:
        with db.session() as session:
            rows = session.scalars(
                select(AccountSnapshotRecord).order_by(AccountSnapshotRecord.timestamp)
            ).all()
        peak_equity: float | None = None
        result: list[dict[str, Any]] = []
        for row in rows:
            peak_equity = row.equity if peak_equity is None else max(peak_equity, row.equity)
            drawdown_percent = (
                (peak_equity - row.equity) / peak_equity * 100 if peak_equity > 0 else None
            )
            result.append(
                {
                    "snapshot_id": row.id,
                    "timestamp": row.timestamp,
                    "equity": row.equity,
                    "balance": row.balance,
                    "drawdown_percent": drawdown_percent,
                }
            )
        # Keep the endpoint backwards-compatible for callers that need the
        # complete immutable series, while allowing dashboards to request a
        # bounded display projection.  Sampling is display-only: account
        # metrics and persisted snapshots remain unchanged.
        return result if display_limit is None else _display_downsample(result, display_limit, value_key="equity")

    @app.get("/api/performance/drawdown")
    def performance_drawdown() -> list[dict[str, Any]]:
        return analytics.drawdown_curve(_trade_samples(completed_trades()))

    @app.get("/api/performance/by-direction")
    def performance_by_direction() -> list[dict[str, Any]]:
        samples = _trade_samples(completed_trades())
        return analytics.segment(samples, lambda trade: trade.direction)

    @app.get("/api/performance/by-session")
    def performance_by_session() -> list[dict[str, Any]]:
        samples = _trade_samples(completed_trades())
        return analytics.segment(samples, lambda trade: trade.session)

    @app.get("/api/performance/by-confidence")
    def performance_by_confidence() -> list[dict[str, Any]]:
        rows = completed_trades()
        with db.session() as session:
            decisions = {
                row.id: row.confidence for row in session.scalars(select(AiDecisionRecord))
            }
        samples = [
            replace(sample, confidence=decisions.get(row.ai_decision_id))
            for row, sample in zip(rows, _trade_samples(rows), strict=True)
        ]
        return analytics.confidence_calibration(samples)

    @app.get("/api/performance/cumulative-r")
    def performance_cumulative_r() -> list[dict[str, Any]]:
        total = 0.0
        result: list[dict[str, Any]] = []
        for trade in _trade_samples(completed_trades()):
            if trade.realized_r is None:
                continue
            total += trade.realized_r
            result.append(
                {
                    "trade_id": trade.trade_id,
                    "timestamp": trade.exit_time,
                    "cumulative_r": total,
                }
            )
        return result

    @app.get("/api/performance/pnl-by-day")
    def performance_pnl_by_day() -> list[dict[str, Any]]:
        groups: dict[str, float] = {}
        for trade in _trade_samples(completed_trades()):
            key = trade.exit_time.date().isoformat()
            groups[key] = groups.get(key, 0.0) + trade.net_profit
        return [{"date": key, "net_profit": value} for key, value in groups.items()]

    @app.get("/api/performance/by-weekday")
    def performance_by_weekday() -> list[dict[str, Any]]:
        samples = _trade_samples(completed_trades())
        return analytics.segment(samples, lambda trade: trade.exit_time.strftime("%A"))

    @app.get("/api/performance/by-hour")
    def performance_by_hour() -> list[dict[str, Any]]:
        samples = _trade_samples(completed_trades())
        return analytics.segment(samples, lambda trade: f"{trade.exit_time.hour:02d}:00 UTC")

    @app.get("/api/performance/monthly")
    def performance_monthly() -> list[dict[str, Any]]:
        samples = _trade_samples(completed_trades())
        return analytics.segment(samples, lambda trade: trade.exit_time.strftime("%Y-%m"))

    @app.get("/api/risk/current")
    def current_risk() -> dict[str, Any] | None:
        with db.session() as session:
            row, market_row = _latest_complete_snapshot(session)
            if row is None:
                row = session.scalar(
                    select(RiskSnapshotRecord).order_by(desc(RiskSnapshotRecord.timestamp)).limit(1)
                )
                market_row = session.scalar(
                    select(MarketSnapshotRecord)
                    .order_by(desc(MarketSnapshotRecord.timestamp))
                    .limit(1)
                )
            if row is None:
                return None
            active_count = (
                session.scalar(
                    select(func.count())
                    .select_from(PositionRecord)
                    .where(PositionRecord.closed_observed_at.is_(None))
                )
                or 0
            )
            freshness, observed_at = _snapshot_freshness(
                row,
                market_row,
                max_age_seconds=runtime_settings.data_stale_position_seconds,
                active_count=active_count,
            )
            payload = {
                column.name: getattr(row, column.name)
                for column in RiskSnapshotRecord.__table__.columns
            }
            payload.update(
                {
                    "observed_at": observed_at,
                    "snapshot_id": row.market_snapshot_id,
                    "freshness": freshness,
                    "read_only": True,
                }
            )
            return payload

    @app.get("/api/system/events")
    def system_events(
        severity: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
        source: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        query: Select[tuple[SystemEventRecord]] = select(SystemEventRecord)
        if severity:
            query = query.where(SystemEventRecord.severity == severity)
        if source:
            query = query.where(SystemEventRecord.source == source)
        with db.session() as session:
            rows = session.scalars(query.order_by(desc(SystemEventRecord.timestamp)).limit(limit))
            return [
                {
                    "event_id": row.event_id,
                    "event_type": row.event_type,
                    "timestamp": row.timestamp,
                    "source": row.source,
                    "severity": row.severity,
                    "correlation_id": row.correlation_id,
                    "payload": row.payload,
                    "schema_version": row.schema_version,
                }
                for row in rows
            ]

    @app.get("/api/system/health")
    def system_health() -> dict[str, Any]:
        now = datetime.now(UTC)
        with db.session() as session:
            records = SystemHealthRepository.latest_records(session)
            latest: dict[str, dict[str, Any]] = {}
            for row in records:
                if row.component not in latest:
                    latest[row.component] = {
                        "component": row.component,
                        "status": row.status,
                        "timestamp": row.timestamp,
                        "latency_ms": row.latency_ms,
                        "message": row.message,
                    }
            event_rows = session.scalars(
                select(SystemEventRecord).order_by(desc(SystemEventRecord.timestamp)).limit(200)
            ).all()
            database_state = "CONNECTED" if db.healthcheck() else "DISCONNECTED"
            supervisor_state = _supervisor_health_state(
                PROJECT_ROOT,
                now,
                max(30.0, runtime_settings.live_account_interval_seconds * 4),
            )
            mt5_state = _service_state_from_events(
                event_rows,
                connected_type="MT5_CONNECTED",
                disconnected_types=("MT5_DISCONNECTED",),
                now=now,
            )
            component_states = {
                row.component.casefold(): row.status.upper()
                for row in records
                if row.component.casefold() in {"telegram", "mt5"}
            }
            telegram_configured = bool(
                runtime_settings.telegram_bot_token
                and runtime_settings.telegram_bot_token.strip()
                and runtime_settings.telegram_chat_id
                and runtime_settings.telegram_chat_id.strip()
            )
            telegram_state = (
                "DISABLED"
                if not runtime_settings.telegram_enabled
                else "ERROR"
                if not telegram_configured
                else component_states.get("telegram", "UNKNOWN")
            )
            runtime_row = latest.get("live_runtime")
            runtime_state = "UNKNOWN"
            if runtime_row is not None:
                runtime_age = (now - runtime_row["timestamp"]).total_seconds()
                runtime_state = (
                    runtime_row["status"]
                    if runtime_age <= max(30.0, runtime_settings.live_account_interval_seconds * 4)
                    else "DEGRADED"
                )
            history_row = latest.get("worker:history")
            history_state = derive_worker_state(
                status=history_row["status"] if history_row else None,
                timestamp=history_row["timestamp"] if history_row else None,
                interval_seconds=runtime_settings.live_history_interval_seconds,
                now=now,
            )
            shadow_row = latest.get("worker:shadow")
            shadow_state = derive_worker_state(
                status=shadow_row["status"] if shadow_row else None,
                timestamp=shadow_row["timestamp"] if shadow_row else None,
                interval_seconds=max(15.0, runtime_settings.live_account_interval_seconds * 4),
                now=now,
            )
            outcome_row = latest.get("worker:shadow_outcome")
            outcome_state = derive_worker_state(
                status=outcome_row["status"] if outcome_row else None,
                timestamp=outcome_row["timestamp"] if outcome_row else None,
                interval_seconds=max(15.0, runtime_settings.live_candle_interval_seconds * 4),
                now=now,
            )
            forward_row = latest.get("worker:forward_shadow")
            forward_state = (
                "DISABLED"
                if not runtime_settings.forward_shadow_enabled
                else derive_worker_state(
                    status=forward_row["status"] if forward_row else None,
                    timestamp=forward_row["timestamp"] if forward_row else None,
                    interval_seconds=runtime_settings.live_history_interval_seconds,
                    now=now,
                )
            )
            last_market_update = session.scalar(
                select(MarketSnapshotRecord.timestamp)
                .order_by(desc(MarketSnapshotRecord.timestamp))
                .limit(1)
            )
            return {
                "database": database_state,
                "services": {
                    "supervisor": supervisor_state,
                    "mt5": component_states.get("mt5", mt5_state),
                    "database": database_state,
                    "telegram": telegram_state,
                    "ai_engine": "PLANNED",
                    "news": "PLANNED",
                    "trade_execution": "DISABLED",
                    "live_engine": runtime_state,
                    "history_worker": history_state,
                    "shadow_worker": shadow_state,
                    "shadow_outcome_worker": outcome_state,
                    "forward_shadow_worker": forward_state,
                },
                "components": list(latest.values()),
                "error_count": session.scalar(
                    select(func.count())
                    .select_from(SystemEventRecord)
                    .where(SystemEventRecord.severity.in_(("ERROR", "CRITICAL")))
                )
                or 0,
                "checked_at": now,
                "started_at": started_at,
                "uptime_seconds": (now - started_at).total_seconds(),
                "last_market_update": last_market_update,
                "websocket_clients": realtime.client_count,
                "database_identity": db.database_identity,
            }

    @app.get("/api/live/status")
    def live_status() -> dict[str, Any]:
        """Expose persisted live-engine state without broker credentials."""

        now = datetime.now(UTC)
        with db.session() as session:
            health_rows = session.scalars(
                select(SystemHealthRecord)
                .where(
                    or_(
                        SystemHealthRecord.component == "live_runtime",
                        SystemHealthRecord.component.like("worker:%"),
                    )
                )
                .order_by(desc(SystemHealthRecord.timestamp))
                .limit(200)
            ).all()
            latest_market = session.scalar(
                select(MarketSnapshotRecord.timestamp)
                .order_by(desc(MarketSnapshotRecord.timestamp))
                .limit(1)
            )
            latest_account = session.scalar(
                select(AccountSnapshotRecord.timestamp)
                .order_by(desc(AccountSnapshotRecord.timestamp))
                .limit(1)
            )
            latest_history = session.scalar(
                select(SystemHealthRecord)
                .where(SystemHealthRecord.component == "worker:history")
                .order_by(desc(SystemHealthRecord.timestamp))
                .limit(1)
            )
        latest: dict[str, SystemHealthRecord] = {}
        for row in health_rows:
            if row.component not in latest:
                latest[row.component] = row

        def freshness(timestamp: datetime | None, threshold: float) -> dict[str, Any]:
            if timestamp is None:
                return {"state": "UNKNOWN", "observed_at": None, "age_seconds": None}
            age = max(0.0, (now - timestamp).total_seconds())
            return {
                "state": "LIVE" if age <= threshold else "STALE",
                "observed_at": timestamp,
                "age_seconds": age,
            }

        runtime = latest.get("live_runtime")
        workers = [
            {
                "name": component.removeprefix("worker:"),
                "state": (
                    derive_worker_state(
                        status=record.status,
                        timestamp=record.timestamp,
                        interval_seconds=(
                            runtime_settings.live_history_interval_seconds
                            if component == "worker:history"
                            else max(15.0, runtime_settings.live_account_interval_seconds * 4)
                            if component == "worker:shadow"
                            else max(15.0, runtime_settings.live_candle_interval_seconds * 4)
                            if component == "worker:shadow_outcome"
                            else runtime_settings.live_history_interval_seconds
                            if component == "worker:forward_shadow"
                            else runtime_settings.live_account_interval_seconds
                        ),
                        now=now,
                    )
                    if component.startswith("worker:")
                    else record.status
                ),
                "last_success_at": (
                    record.metadata_json.get("last_success_at")
                    if record.metadata_json
                    else record.timestamp
                ),
                "last_failed_at": record.metadata_json.get("last_failed_at")
                if record.metadata_json
                else None,
                "failure_count": record.metadata_json.get("failure_count", 0)
                if record.metadata_json
                else 0,
                "message": record.message,
            }
            for component, record in latest.items()
            if component.startswith("worker:")
        ]
        runtime_freshness = freshness(
            runtime.timestamp if runtime else None,
            max(30.0, runtime_settings.live_account_interval_seconds * 4),
        )
        runtime_state = runtime.status if runtime else "UNKNOWN"
        if (
            runtime is not None
            and runtime_freshness["state"] != "LIVE"
            and runtime_state != "STOPPED"
        ):
            runtime_state = "DEGRADED"
        return {
            "state": runtime_state,
            "updated_at": runtime.timestamp if runtime else None,
            "symbol": runtime.metadata_json.get("symbol")
            if runtime and runtime.metadata_json
            else None,
            "freshness": {
                "runtime": runtime_freshness,
                "market": freshness(latest_market, runtime_settings.data_stale_tick_seconds),
                "account": freshness(latest_account, runtime_settings.data_stale_account_seconds),
                "history": freshness(
                    latest_history.timestamp if latest_history else None,
                    worker_health_ttl(runtime_settings.live_history_interval_seconds),
                ),
                "history_worker": worker_health_payload(
                    latest_history,
                    interval_seconds=runtime_settings.live_history_interval_seconds,
                    now=now,
                ),
                "shadow_worker": worker_health_payload(
                    latest.get("worker:shadow"),
                    interval_seconds=max(15.0, runtime_settings.live_account_interval_seconds * 4),
                    now=now,
                ),
                "shadow_outcome_worker": worker_health_payload(
                    latest.get("worker:shadow_outcome"),
                    interval_seconds=max(15.0, runtime_settings.live_candle_interval_seconds * 4),
                    now=now,
                ),
                "forward_shadow_worker": forward_health(db, runtime_settings),
            },
            "workers": workers,
            "read_only": True,
        }

    @app.get("/api/config/public")
    def public_config() -> dict[str, Any]:
        return {
            "trading_symbol_override": runtime_settings.trading_symbol,
            "telegram_enabled": runtime_settings.telegram_enabled,
            "telegram_control_enabled": runtime_settings.telegram_control_enabled,
            "max_trade_risk_percent": runtime_settings.max_trade_risk_percent,
            "max_aggregate_risk_percent": runtime_settings.max_aggregate_risk_percent,
            "shadow_engine_enabled": runtime_settings.shadow_engine_enabled,
            "shadow_strategy": runtime_settings.shadow_strategy,
            "shadow_decision_timeframe": runtime_settings.shadow_decision_timeframe,
            "shadow_min_rr": runtime_settings.shadow_min_rr,
            "shadow_notify_signals": runtime_settings.shadow_notify_signals,
            "shadow_notify_no_trade": runtime_settings.shadow_notify_no_trade,
            "candle_counts": runtime_settings.candle_counts,
            "read_only": True,
            "timezone": "UTC",
        }

    @app.get("/api/supervisor/status")
    def supervisor_status() -> dict[str, Any]:
        registry_path = PROJECT_ROOT / "data" / "process_registry.json"
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            registry = {}
        safe: dict[str, Any] = {}
        for component, raw_record in registry.items() if isinstance(registry, dict) else ():
            record = raw_record if isinstance(raw_record, dict) else {}
            try:
                identity_record = ProcessRecord(
                    component=component,
                    pid=record.get("pid"),
                    state=str(record.get("state", "UNKNOWN")),
                    started_at=record.get("started_at"),
                    last_heartbeat=record.get("last_heartbeat"),
                    exit_code=record.get("exit_code"),
                    desired_state=str(
                        record.get("desired_state")
                        or (
                            "RUNNING"
                            if record.get("state") in {"RUNNING", "STARTING"}
                            else "STOPPED"
                        )
                    ),
                    restart_count=int(record.get("restart_count", 0)),
                    last_error=record.get("last_error"),
                    process_create_time=record.get("process_create_time"),
                    parent_pid=record.get("parent_pid"),
                    command=tuple(record.get("command") or ()),
                    process_tree=tuple(record.get("process_tree") or ()),
                    process_identities=tuple(
                        (int(item[0]), item[1])
                        for item in (record.get("process_identities") or ())
                        if isinstance(item, (list, tuple)) and len(item) == 2
                    ),
                )
            except (TypeError, ValueError):
                identity_record = None
            verified = bool(identity_record and record_process_is_alive(identity_record))
            safe[component] = {
                "pid": record.get("pid"),
                "state": (
                    "DEGRADED"
                    if record.get("state") in {"RUNNING", "STARTING"} and not verified
                    else record.get("state", "UNKNOWN")
                ),
                "verified": verified,
                "started_at": record.get("started_at"),
                "last_heartbeat": record.get("last_heartbeat"),
                "exit_code": record.get("exit_code"),
                "desired_state": record.get(
                    "desired_state",
                    "RUNNING" if record.get("state") in {"RUNNING", "STARTING"} else "STOPPED",
                ),
                "restart_count": record.get("restart_count", 0),
            }
        return {"components": safe, "read_only": True, "execution": "DISABLED"}

    @app.get("/api/export/trades.csv")
    def export_trades() -> StreamingResponse:
        rows = [_trade_dict(row) for row in completed_trades()]
        return _csv_response("trades.csv", rows)

    @app.get("/api/export/decisions.csv")
    def export_decisions() -> StreamingResponse:
        with db.session() as session:
            rows = [
                {
                    "decision_id": row.id,
                    "timestamp": row.timestamp,
                    "action": row.action,
                    "confidence": row.confidence,
                    "validation_status": row.validation_status,
                    "execution_status": row.execution_status,
                    "model_name": row.model_name,
                    "model_version": row.model_version,
                    "prompt_version": row.prompt_version,
                }
                for row in session.scalars(
                    select(AiDecisionRecord).order_by(AiDecisionRecord.timestamp)
                )
            ]
        return _csv_response("ai_decisions.csv", rows)

    @app.get("/api/export/trade-events.csv")
    def export_trade_events() -> StreamingResponse:
        with db.session() as session:
            rows = [
                {
                    "event_id": row.id,
                    "trade_id": row.trade_id,
                    "timestamp": row.timestamp,
                    "event_type": row.event_type,
                    "price": row.price,
                    "volume": row.volume,
                    "stop_loss": row.stop_loss,
                    "take_profit": row.take_profit,
                    "pnl": row.pnl,
                    "reason": row.reason,
                    "source": row.source,
                }
                for row in session.scalars(
                    select(TradeEventRecord).order_by(TradeEventRecord.timestamp)
                )
            ]
        return _csv_response("trade_events.csv", rows)

    @app.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket) -> None:
        await realtime.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            realtime.disconnect(websocket)

    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="dashboard")
    return app
