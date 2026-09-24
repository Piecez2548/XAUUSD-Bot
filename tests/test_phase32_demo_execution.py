from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import Settings
from models.history import DealFact
from models.market import AccountState, Candle, MarketSnapshot, SymbolSpecification, Tick, Timeframe
from models.shadow import ShadowAction
from persistence.database import Database
from persistence.orm import (
    DemoExecutionRecord,
    ForwardSignalRecord,
    ForwardValidationSessionRecord,
    StrategyIntelligenceRecord,
)
from persistence.repositories import HistoryRepository
from services.demo_execution import (
    DemoExecutionService,
    set_demo_execution_enabled,
)
from services.forward_shadow import ForwardInput, ForwardShadowWorker


class FakeGateway:
    def __init__(self, api) -> None:
        self.api = api
        self.connected = True

    async def call(self, operation):
        if not self.connected:
            raise RuntimeError("MT5 gateway is disconnected")
        return operation(self.api)


class FakeApi:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008

    def __init__(self, *, trade_mode: int = 0, retcode: int = TRADE_RETCODE_DONE) -> None:
        now = datetime.now(UTC)
        self.trade_mode = trade_mode
        self.retcode = retcode
        self.tick_time = int(now.timestamp())
        self.order_calls = 0
        self.positions = ()
        self.symbol = SimpleNamespace(
            name="XAUUSDm",
            bid=2000.0,
            ask=2000.2,
            spread=20,
            digits=2,
            point=0.01,
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            trade_tick_value_profit=1.0,
            trade_tick_value_loss=1.0,
            trade_contract_size=100.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_mode=4,
            filling_mode=2,
            visible=True,
        )

    def symbol_info(self, _symbol):
        return self.symbol

    def symbol_info_tick(self, _symbol):
        return SimpleNamespace(
            time=self.tick_time,
            bid=self.symbol.bid,
            ask=self.symbol.ask,
            last=self.symbol.bid,
            volume=1,
            flags=0,
        )

    def account_info(self):
        return SimpleNamespace(
            balance=10_000,
            equity=10_000,
            margin=0,
            margin_free=10_000,
            margin_level=0,
            profit=0,
            leverage=100,
            currency="USD",
            server="Demo-Server",
            trade_mode=self.trade_mode,
        )

    def positions_get(self, *, symbol):
        assert symbol == "XAUUSDm"
        return self.positions

    def order_send(self, _request):
        self.order_calls += 1
        return SimpleNamespace(
            retcode=self.retcode,
            order=101,
            deal=202,
            position=303,
            volume=0.1,
            price=self.symbol.ask,
            comment="accepted",
            request_id=404,
            retcode_external=0,
        )


def _database(tmp_path) -> Database:
    database = Database(f"sqlite:///{(tmp_path / 'demo-execution.db').as_posix()}")
    database.create_schema()
    set_demo_execution_enabled(database, True, reason="TEST_ARM", updated_by="test")
    return database


def _snapshot(api: FakeApi, *, account_mode: int = 0) -> MarketSnapshot:
    now = datetime.now(UTC)
    account = AccountState(
        balance=10_000,
        equity=10_000,
        margin=0,
        free_margin=10_000,
        margin_level=0,
        profit=0,
        leverage=100,
        currency="USD",
        server="Demo-Server",
        trade_mode=account_mode,
        trade_mode_name="demo" if account_mode == 0 else "real",
    )
    symbol = SymbolSpecification(
        name="XAUUSDm",
        bid=2000.0,
        ask=2000.2,
        spread=20,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        trade_tick_value_profit=1.0,
        trade_tick_value_loss=1.0,
        contract_size=100,
        volume_min=0.01,
        volume_max=100,
        volume_step=0.01,
        trade_mode=4,
        trade_mode_name="full",
    )
    tick = Tick(
        timestamp=now,
        raw_timestamp=int(now.timestamp()),
        bid=2000.0,
        ask=2000.2,
        last=2000.0,
        volume=1,
        flags=0,
    )
    candles = {
        timeframe: (
            Candle(
                timestamp=now - timedelta(minutes=10),
                raw_timestamp=int((now - timedelta(minutes=10)).timestamp()),
                open=1999,
                high=2001,
                low=1998,
                close=2000,
                tick_volume=100,
                spread=20,
                real_volume=0,
            ),
        )
        for timeframe in Timeframe
    }
    return MarketSnapshot(
        account=account,
        symbol=symbol,
        tick=tick,
        positions=(),
        candles=candles,
        generated_at=now,
    )


def _graph(
    database: Database, *, direction: str = "BUY", stop: float = 1990.0, target: float = 2020.0
):
    now = datetime.now(UTC)
    with database.session() as session:
        forward_session = ForwardValidationSessionRecord(
            session_id="forward-demo-test",
            strategy_id="pair_zone_v1",
            strategy_version="1.0.0",
            strategy_config_hash="a" * 64,
            started_at=now - timedelta(minutes=10),
            source_identity="fixture",
            symbol="XAUUSDm",
            timeframes_json=["M5", "M15", "H1"],
            rr=2.0,
            cost_policy_json={},
            status="ACTIVE",
            execution_allowed=False,
        )
        session.add(forward_session)
        session.flush()
        signal = ForwardSignalRecord(
            signal_id="forward-signal-demo-test",
            session_id=forward_session.id,
            timestamp=now - timedelta(minutes=1),
            decision=direction,
            zone_id="pz-demo-test",
            entry_price=2000.2,
            stop_loss=stop,
            risk_distance=abs(2000.2 - stop),
            rr=2.0,
            take_profit=target,
            strategy_hash="b" * 64,
            execution_allowed=False,
        )
        session.add(signal)
        session.flush()
        intelligence = StrategyIntelligenceRecord(
            candidate_id="candidate-demo-test",
            symbol="XAUUSDm",
            strategy="pair_zone_v1",
            strategy_version="1.0.0",
            intelligence_version="phase3.0_intelligence_v1",
            intelligence_runtime_version="phase3.0_intelligence_v1",
            evidence_version="evidence_v1",
            detected_at=signal.timestamp,
            timeframe="M15/M5",
            direction=direction,
            state="CONFIRMED",
            score=70,
            confidence_band="HIGH",
            alert_decision="ALERT",
            blockers_json=[],
            warnings_json=[],
            context_json={},
            evidence_json=[],
            score_components_json=[],
            source="ExactPairAdapter",
            pair_zone_event_id=signal.zone_id,
            forward_session_id=forward_session.id,
            forward_signal_id=signal.id,
            execution_allowed=False,
        )
        session.add(intelligence)
        session.flush()
        return signal, intelligence


def _service(database, api: FakeApi, *, enabled: bool = True) -> DemoExecutionService:
    settings = Settings(
        database_url="sqlite:///:memory:",
        demo_execution_enabled=enabled,
        demo_execution_max_tick_age_seconds=10,
        demo_execution_max_entry_deviation_points=50,
    )
    return DemoExecutionService(
        settings, database, FakeGateway(api), logger=__import__("logging").getLogger("demo-test")
    )


def _decision(direction: str = "BUY") -> SimpleNamespace:
    return SimpleNamespace(decision=ShadowAction(direction))


@pytest.mark.asyncio
async def test_successful_demo_submission_is_persisted_once(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, intelligence = _graph(database)
    result = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    repeat = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert result is not None and result.status == "ACKNOWLEDGED"
    assert repeat is not None and repeat.id == result.id
    assert api.order_calls == 1
    assert result.execution_mode == "DEMO"
    assert result.broker_position_ticket == 303
    database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("non_demo", lambda api, snapshot: setattr(api, "trade_mode", 2)),
        ("disconnected", lambda api, snapshot: None),
        ("stale", lambda api, snapshot: setattr(api, "tick_time", 1)),
        ("invalid_sl", lambda api, snapshot: None),
        ("invalid_tp", lambda api, snapshot: None),
        ("oversized_min_lot", lambda api, snapshot: setattr(api.symbol, "volume_min", 1000.0)),
    ],
)
async def test_required_safety_gate_rejects_without_order(tmp_path, case: str, mutate) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, intelligence = _graph(
        database,
        stop=2001.0 if case == "invalid_sl" else 1990.0,
        target=1999.0 if case == "invalid_tp" else 2020.0,
    )
    snapshot = _snapshot(api, account_mode=2 if case == "non_demo" else 0)
    if case == "disconnected":
        service = _service(database, api)
        service.gateway.connected = False
    else:
        service = _service(database, api)
    mutate(api, snapshot)
    result = await service.execute(
        signal=signal,
        decision=_decision(),
        snapshot=snapshot,
        intelligence_record=intelligence,
    )
    assert result is not None and result.status == "REJECTED"
    assert api.order_calls == 0
    assert result.rejection_reason
    database.dispose()


@pytest.mark.asyncio
async def test_invalid_pair_zone_signal_and_missing_sl_fail_closed(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, intelligence = _graph(database)
    signal.decision = "NO_TRADE"
    invalid = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert invalid is not None and invalid.rejection_reason == "CANONICAL_SIGNAL_INVALID"
    assert api.order_calls == 0

    database2 = _database(tmp_path / "missing-sl")
    api2 = FakeApi()
    signal2, intelligence2 = _graph(database2)
    signal2.stop_loss = None
    missing = await _service(database2, api2).execute(
        signal=signal2,
        decision=_decision(),
        snapshot=_snapshot(api2),
        intelligence_record=intelligence2,
    )
    assert missing is not None and missing.rejection_reason == "TRADE_LEVEL_MISSING"
    assert api2.order_calls == 0
    database.dispose()
    database2.dispose()


@pytest.mark.asyncio
async def test_excessive_aggregate_risk_fails_closed(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    api.positions = (
        SimpleNamespace(
            ticket=77,
            symbol="XAUUSDm",
            type=0,
            volume=0.6,
            price_open=1990.0,
            price_current=2000.0,
            sl=1980.0,
            tp=2020.0,
            profit=0.0,
            swap=0.0,
            magic=0,
            comment="existing-demo",
            time=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
        ),
    )
    signal, intelligence = _graph(database)
    result = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert result is not None and result.rejection_reason == "RISK_BUDGET_EXCEEDED"
    assert api.order_calls == 0
    database.dispose()


@pytest.mark.asyncio
async def test_environment_execution_gate_disabled_is_hard_off(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, intelligence = _graph(database)
    result = await _service(database, api, enabled=False).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert result is None
    assert api.order_calls == 0
    with database.session() as session:
        assert session.query(DemoExecutionRecord).count() == 0
    database.dispose()


@pytest.mark.asyncio
async def test_forward_shadow_bridge_preserves_canonical_decision(tmp_path, monkeypatch) -> None:
    database = _database(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'bridge.db').as_posix()}",
        forward_shadow_enabled=True,
    )
    calls: list[dict[str, object]] = []

    async def execute(**kwargs):
        calls.append(kwargs)

    worker = ForwardShadowWorker(
        settings,
        database,
        logger=logging.getLogger("demo-bridge-test"),
        execution_handler=execute,
    )
    now = datetime.now(UTC)
    worker.session = SimpleNamespace(id="session-id", started_at=now - timedelta(minutes=5))
    decision = SimpleNamespace(
        decision=ShadowAction.BUY,
        m5_candle_timestamp=now,
        entry_price=2000.2,
        stop_loss=1990.0,
        take_profit=2020.0,
        feature_context={"zone_id": "pz-demo-test", "zone_created_at": now.isoformat()},
    )
    monkeypatch.setattr(worker, "_ensure_session_symbol", lambda _symbol: None)
    monkeypatch.setattr(worker.strategy, "evaluate", lambda *args, **kwargs: decision)
    monkeypatch.setattr(
        worker,
        "_persist_signal",
        lambda *_args, **_kwargs: SimpleNamespace(id="signal-id", timestamp=now),
    )
    monkeypatch.setattr(worker, "_evaluate_open_trades", lambda: asyncio.sleep(0))
    monkeypatch.setattr(worker, "_record_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "services.intelligence.persist_intelligence_record",
        lambda *_args, **_kwargs: SimpleNamespace(candidate_id="candidate-id"),
    )
    monkeypatch.setattr(
        "services.intelligence.link_intelligence_forward_provenance",
        lambda *_args, **_kwargs: None,
    )
    snapshot = _snapshot(FakeApi())
    await worker._process(ForwardInput(snapshot=snapshot, market_snapshot_id="market", risk=None))
    assert decision.decision is ShadowAction.BUY
    assert len(calls) == 1
    assert calls[0]["signal"].id == "signal-id"
    assert calls[0]["intelligence_record"].candidate_id == "candidate-id"
    database.dispose()


@pytest.mark.asyncio
async def test_observed_close_deal_completes_demo_audit_chain(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, intelligence = _graph(database)
    result = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert result is not None and result.broker_position_ticket == 303
    close_time = datetime.now(UTC) + timedelta(seconds=1)
    fact = DealFact(
        deal_ticket=505,
        order_ticket=101,
        position_id=303,
        symbol="XAUUSDm",
        timestamp=close_time,
        deal_type="DEAL",
        entry_type="OUT",
        volume=0.1,
        price=2019.0,
        profit=100.0,
        reason="TP",
        comment="XAUDEMO-close",
        magic_number=3031101,
    )
    assert HistoryRepository(database).persist_deals((fact,), scope="deals:XAUUSDm") == 1
    with database.session() as session:
        stored = session.get(DemoExecutionRecord, result.id)
        assert stored is not None
        assert stored.status == "CLOSED"
        assert stored.terminal_status == "TP"
        assert stored.terminal_outcome_at == close_time
    database.dispose()


@pytest.mark.asyncio
async def test_missing_or_ambiguous_provenance_fails_closed(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, _intelligence = _graph(database)
    result = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=None,
    )
    assert result is not None and result.rejection_reason == "INTELLIGENCE_PROVENANCE_MISSING"
    assert api.order_calls == 0
    database.dispose()


@pytest.mark.asyncio
async def test_broker_rejection_is_persisted_and_not_retried(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi(retcode=10016)
    signal, intelligence = _graph(database)
    service = _service(database, api)
    result = await service.execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    repeat = await service.execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert result is not None and result.status == "REJECTED"
    assert repeat is not None and repeat.id == result.id
    assert api.order_calls == 1
    database.dispose()


@pytest.mark.asyncio
async def test_indeterminate_submission_is_never_retried_after_restart(tmp_path) -> None:
    database = _database(tmp_path)
    api = FakeApi()
    signal, intelligence = _graph(database)
    api.order_send = lambda _request: (_ for _ in ()).throw(RuntimeError("transport lost"))
    first = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    second = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert first is not None and first.status == "INDETERMINATE"
    assert second is not None and second.id == first.id
    assert api.order_calls == 0
    database.dispose()


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_orders_without_closing_positions(tmp_path) -> None:
    database = _database(tmp_path)
    set_demo_execution_enabled(database, False, reason="TEST_KILL", updated_by="test")
    api = FakeApi()
    signal, intelligence = _graph(database)
    result = await _service(database, api).execute(
        signal=signal,
        decision=_decision(),
        snapshot=_snapshot(api),
        intelligence_record=intelligence,
    )
    assert result is None
    assert api.order_calls == 0
    with database.session() as session:
        assert session.query(DemoExecutionRecord).count() == 0
    database.dispose()
