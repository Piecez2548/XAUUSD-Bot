from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.app import create_app
from config.settings import Settings
from models.shadow import MarketRegime, ShadowAction, ShadowDecision
from persistence.database import Database
from persistence.orm import (
    Base,
    CandleRecord,
    ResearchDatasetRecord,
    ResearchRobustnessRecord,
    ResearchRunRecord,
    SymbolRecord,
)
from services.control import TelegramControlService
from services.research_platform import (
    _rr_decision,
    build_split_definition,
    build_walk_forward_windows,
    import_research_dataset,
)


def _database_with_candles(path: str = "sqlite:///:memory:") -> Database:
    database = Database(path)
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        symbol = SymbolRecord(
            name="XAUUSD", digits=2, point=0.01, trade_tick_size=0.01,
            trade_tick_value=1, trade_tick_value_profit=1, trade_tick_value_loss=1,
            contract_size=100, volume_min=0.01, volume_max=100, volume_step=0.01,
            trade_mode=4, trade_mode_name="full",
        )
        session.add(symbol)
        session.flush()
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(4):
            timestamp = start + timedelta(minutes=5 * index)
            session.add(CandleRecord(
                symbol_id=symbol.id, timeframe="M5", timestamp=timestamp,
                raw_timestamp=int(timestamp.timestamp()), open=2000 + index,
                high=2002 + index, low=1999 + index, close=2001 + index,
                tick_volume=100, spread=20, real_volume=0,
            ))
    return database


def test_research_import_is_immutable_and_deduplicated():
    database = _database_with_candles()
    first = import_research_dataset(database)
    second = import_research_dataset(database)
    assert first.dataset_id == second.dataset_id
    assert first.row_count == 4
    assert first.dataset_hash == second.dataset_hash
    database.dispose()


def test_rr_copies_reuse_signal_geometry_with_owned_ids():
    canonical = ShadowDecision(
        symbol="XAUUSD", m5_candle_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        decision=ShadowAction.BUY, market_regime=MarketRegime.TREND_UP,
        entry_price=2000.0, stop_loss=1990.0, take_profit=2020.0,
        risk_reward_ratio=2.0, hypothetical_volume=0.01, confidence=0.6,
        human_readable_reason="test",
        risk_gate_state="APPROVED", data_freshness="FRESH",
    )
    rr_one = _rr_decision(canonical, 1.0)
    rr_three = _rr_decision(canonical, 3.0)
    assert rr_one.decision_id != rr_three.decision_id
    assert rr_one.decision_id != canonical.decision_id
    assert rr_one.stop_loss == canonical.stop_loss == rr_three.stop_loss
    assert rr_one.take_profit == 2010.0
    assert rr_three.take_profit == 2030.0


def test_research_split_and_walk_forward_are_chronological():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 3, 1, tzinfo=UTC)
    split = build_split_definition(start, end)
    assert split["policy"] == "chronological_60_20_20"
    assert split["status"] == "INSUFFICIENT_DATA_FOR_HOLDOUT_VALIDATION"
    windows = build_walk_forward_windows(start, end, train_days=30, test_days=7)
    assert windows
    assert windows[0]["train_end"] == windows[0]["test_start"]


def test_research_dataset_api_exposes_manifest_without_secrets(tmp_path):
    database = _database_with_candles(f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    imported = import_research_dataset(database)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    # The API database is deliberately the same seeded database object.
    client = TestClient(create_app(settings=settings, database=database))
    response = client.get("/api/research/datasets")
    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["dataset_id"] == imported.dataset_id
    assert "telegram_bot_token" not in response.text
    database.dispose()


def test_research_funnel_api_uses_persisted_aggregate(tmp_path):
    database = _database_with_candles(f"sqlite:///{(tmp_path / 'funnel.db').as_posix()}")
    imported = import_research_dataset(database)
    with database.session() as session:
        dataset = session.scalar(select(ResearchDatasetRecord).where(
            ResearchDatasetRecord.dataset_id == imported.dataset_id
        ))
        session.add(ResearchRunRecord(
            run_id="funnel-run", run_name="test", strategy_id="trend_pullback_v1",
            strategy_version="1.0.0", config_hash="config", dataset_id=dataset.id,
            dataset_hash=imported.dataset_hash, symbol=imported.symbol, timeframe="M5",
            status="COMPLETED", engine_version="test", parameters_json={"rr": 2.0},
            split_definition={}, summary_json={
                "eligible_candles": 4, "buy": 1, "sell": 1, "no_trade": 2,
                "reason_counts": {"HTF_TREND_NOT_ALIGNED": 2, "VALID_TREND_PULLBACK": 2},
                "funnel": {"snapshots": 4, "decisions": 2, "eligible": 2,
                            "no_trade_aggregate": 2},
            }, execution_allowed=False,
        ))
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'funnel.db').as_posix()}")
    client = TestClient(create_app(settings=settings, database=database))
    payload = client.get("/api/research/runs/funnel-run/funnel").json()
    assert payload["snapshots"] == 4
    assert payload["decisions"] == 2
    assert payload["no_trade_aggregate"] == 2
    assert payload["reason_counts"]["HTF_TREND_NOT_ALIGNED"] == 2
    assert payload["execution_allowed"] is False
    comparison = client.get(
        "/api/research/compare?strategy_id=trend_pullback_v1&rr=2.0"
    ).json()
    assert len(comparison) == 1
    curve = client.get("/api/research/runs/funnel-run/curve").json()
    assert curve["equity"] == []
    assert curve["execution_allowed"] is False
    database.dispose()


def test_robustness_api_exposes_one_persisted_read_only_source(tmp_path):
    database = _database_with_candles(f"sqlite:///{(tmp_path / 'robustness.db').as_posix()}")
    imported = import_research_dataset(database)
    with database.session() as session:
        dataset = session.scalar(select(ResearchDatasetRecord).where(
            ResearchDatasetRecord.dataset_id == imported.dataset_id
        ))
        session.add(ResearchRobustnessRecord(
            robustness_id="robustness-test", strategy_id="pair_zone_v1", strategy_version="1.0.0",
            config_hash="a" * 64, dataset_id=dataset.id, dataset_hash=imported.dataset_hash,
            source_run_id=None, status="COMPLETED", seed=42, simulation_count=10,
            parameters_json={"execution_allowed": False},
            summary_json={
                "acceptance": "ROBUSTNESS TESTS COMPLETE",
                "cost_scenarios": {"normal": {"net_total_r": 1.0}},
            },
            execution_allowed=False,
        ))
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'robustness.db').as_posix()}")
    client = TestClient(create_app(settings=settings, database=database))
    response = client.get("/api/research/robustness?strategy_id=pair_zone_v1")
    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["robustness_id"] == "robustness-test"
    assert payload["summary"]["cost_scenarios"]["normal"]["net_total_r"] == 1.0
    assert payload["execution_allowed"] is False
    assert "telegram_bot_token" not in response.text
    control = TelegramControlService(settings, Path.cwd(), database=database)
    assert "ROBUSTNESS TESTS COMPLETE" in control._robustness()
    assert "net=1.0" in control._costs()
    assert "No persisted" not in control._stability()
    database.dispose()
