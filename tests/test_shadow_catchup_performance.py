from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

import services.shadow_service as shadow_service_module
from config.settings import Settings
from events.bus import EventBus
from models.market import MarketSnapshot
from persistence.database import Database
from persistence.repositories import ShadowDecisionRepository, SnapshotRepository
from services.risk import calculate_risk_snapshot
from services.shadow_replay import (
    PersistedSnapshotPage,
    SnapshotCursor,
    load_persisted_snapshot_page,
)
from services.shadow_service import ShadowDecisionWorker, ShadowInput
from tests.test_phase20_shadow import _trend_snapshot


@pytest.fixture
def catchup_database(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'catchup.db').as_posix()}")
    database.create_schema()
    yield database
    database.dispose()


@pytest.fixture
def market_snapshot(model_parts):
    return _trend_snapshot(model_parts)


def _worker(database):
    return ShadowDecisionWorker(
        Settings(),
        database,
        EventBus(logging.getLogger("shadow-catchup-test")),
        logger=logging.getLogger("shadow-catchup-test"),
    )


def _with_offset(snapshot: MarketSnapshot, index: int) -> MarketSnapshot:
    offset = timedelta(minutes=index * 5)
    candles = {
        timeframe: tuple(
            candle.model_copy(update={"timestamp": candle.timestamp + offset}) for candle in values
        )
        for timeframe, values in snapshot.candles.items()
    }
    return snapshot.model_copy(
        update={
            "candles": candles,
            "generated_at": snapshot.generated_at + timedelta(seconds=index),
        }
    )


def _inputs(base: MarketSnapshot, count: int):
    return [
        (
            _with_offset(base, index),
            UUID(int=index + 1),
            calculate_risk_snapshot(
                _with_offset(base, index),
                max_trade_risk_percent=2,
                max_aggregate_risk_percent=6,
            ),
        )
        for index in range(count)
    ]


def test_snapshot_page_empty_and_small_history_are_bounded(catchup_database, market_snapshot):
    empty = load_persisted_snapshot_page(catchup_database, limit=4)
    assert empty.rows == ()
    assert empty.rows_examined == 0
    assert empty.next_cursor is None
    assert empty.has_more is False

    SnapshotRepository(catchup_database).persist(
        market_snapshot,
        calculate_risk_snapshot(
            market_snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
        ),
    )
    page = load_persisted_snapshot_page(catchup_database, limit=4)
    assert page.rows_examined == 1
    assert len(page.rows) == 1
    assert page.has_more is False
    assert page.next_cursor is not None


def test_snapshot_page_keyset_walk_has_no_skip_or_duplicate(catchup_database, market_snapshot):
    repository = SnapshotRepository(catchup_database)
    for index in range(11):
        snapshot = _with_offset(market_snapshot, index)
        repository.persist(
            snapshot,
            calculate_risk_snapshot(
                snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
            ),
        )

    cursor = None
    seen_ids = []
    page_sizes = []
    while True:
        page = load_persisted_snapshot_page(catchup_database, limit=3, after=cursor)
        page_sizes.append(page.rows_examined)
        seen_ids.extend(str(snapshot_id) for _snapshot, snapshot_id, _risk in page.rows)
        cursor = page.next_cursor
        if not page.has_more:
            break

    assert len(seen_ids) == 11
    assert len(set(seen_ids)) == 11
    assert page_sizes == [3, 3, 3, 2]
    assert max(page_sizes) <= 3


def test_snapshot_page_keyset_walk_handles_equal_timestamps(
    catchup_database, market_snapshot
):
    repository = SnapshotRepository(catchup_database)
    risk = calculate_risk_snapshot(
        market_snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
    )
    for _ in range(5):
        repository.persist(market_snapshot, risk)

    cursor = None
    seen_ids = []
    while True:
        page = load_persisted_snapshot_page(catchup_database, limit=2, after=cursor)
        seen_ids.extend(str(snapshot_id) for _snapshot, snapshot_id, _risk in page.rows)
        cursor = page.next_cursor
        if not page.has_more:
            break

    assert len(seen_ids) == 5
    assert len(set(seen_ids)) == 5


def test_risk_snapshot_lookup_uses_supporting_index(catchup_database):
    with catchup_database.engine.connect() as connection:
        details = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM risk_snapshots "
            "WHERE market_snapshot_id = 'snapshot-id'"
        ).all()
    details = [str(row[3]) for row in details]
    assert any("ix_risk_snapshots_market_snapshot_id" in detail for detail in details)
    assert not any("SCAN risk_snapshots" in detail for detail in details)


def test_catchup_ties_preserve_snapshot_time_with_stable_id_tiebreaker(
    catchup_database, market_snapshot
):
    worker = _worker(catchup_database)
    older = market_snapshot.model_copy(update={"generated_at": market_snapshot.generated_at})
    newer = market_snapshot.model_copy(
        update={"generated_at": market_snapshot.generated_at + timedelta(seconds=1)}
    )
    older_id = UUID(int=99)
    newer_id = UUID(int=1)
    page = PersistedSnapshotPage(
        ((newer, newer_id, None), (older, older_id, None)),
        SnapshotCursor(newer.generated_at, str(newer_id)),
        False,
        2,
    )

    ordered = worker._order_catch_up_rows(page)

    assert [snapshot_id for _snapshot, snapshot_id, _risk in ordered] == [older_id, newer_id]


@pytest.mark.asyncio
async def test_catchup_progresses_across_pages_in_order_without_duplicates(
    catchup_database, market_snapshot, monkeypatch
):
    worker = _worker(catchup_database)
    worker._catchup_page_size = 3
    rows = _inputs(market_snapshot, 8)
    cursor_by_id = {
        str(snapshot_id): SnapshotCursor(snapshot.generated_at, str(snapshot_id))
        for snapshot, snapshot_id, _risk in rows
    }
    page_calls = []

    def load_page(_database, *, limit, after):
        start = (
            0
            if after is None
            else next(
                index + 1
                for index, (_snapshot, snapshot_id, _risk) in enumerate(rows)
                if cursor_by_id[str(snapshot_id)] == after
            )
        )
        page_calls.append((start, limit))
        selected = rows[start : start + limit]
        next_cursor = cursor_by_id[str(selected[-1][1])] if selected else after
        return PersistedSnapshotPage(
            tuple(selected),
            next_cursor,
            start + len(selected) < len(rows),
            len(selected),
        )

    monkeypatch.setattr(shadow_service_module, "load_persisted_snapshot_page", load_page)
    processed = []
    for _ in range(20):
        if worker._needs_catchup:
            await worker._catch_up()
        while not worker._queue.empty():
            item = worker._queue.get_nowait()
            key = worker._key(item)
            worker._queued_keys.discard(key)
            worker._completed_keys.add(key)
            worker._catchup_pending_keys.discard(key)
            processed.append(key)
        if not worker._needs_catchup and worker._queue.empty():
            break

    assert len(processed) == len(rows)
    assert len(set(processed)) == len(rows)
    assert [key[1] for key in processed] == sorted(key[1] for key in processed)
    assert [start for start, _limit in page_calls] == [0, 3, 6]
    assert all(limit == 3 for _start, limit in page_calls)


@pytest.mark.asyncio
async def test_large_history_keeps_each_catchup_page_and_queue_batch_bounded(
    catchup_database, market_snapshot, monkeypatch
):
    worker = _worker(catchup_database)
    worker._catchup_page_size = 17
    rows = _inputs(market_snapshot, 137)
    cursor_by_id = {
        str(snapshot_id): SnapshotCursor(snapshot.generated_at, str(snapshot_id))
        for snapshot, snapshot_id, _risk in rows
    }
    requested_limits = []

    def load_page(_database, *, limit, after):
        requested_limits.append(limit)
        start = 0 if after is None else next(
            index + 1
            for index, (_snapshot, snapshot_id, _risk) in enumerate(rows)
            if cursor_by_id[str(snapshot_id)] == after
        )
        selected = rows[start : start + limit]
        cursor = cursor_by_id[str(selected[-1][1])] if selected else after
        return PersistedSnapshotPage(
            tuple(selected), cursor, start + len(selected) < len(rows), len(selected)
        )

    monkeypatch.setattr(shadow_service_module, "load_persisted_snapshot_page", load_page)
    visited = []
    for _ in range(250):
        if worker._needs_catchup:
            await worker._catch_up()
        assert worker.queue_depth <= 2
        while not worker._queue.empty():
            item = worker._queue.get_nowait()
            key = worker._key(item)
            worker._queued_keys.discard(key)
            worker._completed_keys.add(key)
            visited.append(key)
        if not worker._needs_catchup and worker._queue.empty():
            break

    assert len(visited) == 137
    assert len(set(visited)) == 137
    assert requested_limits
    assert set(requested_limits) == {17}
    assert max(worker.catchup_pending_count, worker.queue_depth) <= 17


@pytest.mark.asyncio
async def test_catchup_preserves_partial_queue_and_full_queue_deferral(
    catchup_database, market_snapshot, monkeypatch
):
    worker = _worker(catchup_database)
    rows = _inputs(market_snapshot, 4)
    calls = []

    def load_page(_database, *, limit, after):
        calls.append((limit, after))
        if after is not None:
            return PersistedSnapshotPage((), after, False, 0)
        last_snapshot, last_id, _risk = rows[-1]
        return PersistedSnapshotPage(
            tuple(rows), SnapshotCursor(last_snapshot.generated_at, str(last_id)), False, 4
        )

    monkeypatch.setattr(shadow_service_module, "load_persisted_snapshot_page", load_page)
    live_item = ShadowInput(market_snapshot, None, None, True)
    live_key = worker._key(live_item)
    worker._queue.put_nowait(live_item)
    worker._queued_keys.add(live_key)
    await worker._catch_up()
    assert worker.queue_depth == 3
    assert worker._queue.get_nowait() is live_item

    # Fill the queue and prove catch-up defers without loading/reconstructing.
    for index in range(worker._queue.maxsize - worker.queue_depth):
        item = ShadowInput(_with_offset(market_snapshot, index + 20), None, None, True)
        worker._queue.put_nowait(item)
    before = len(calls)
    worker._needs_catchup = True
    await worker._catch_up()
    assert len(calls) == before
    assert worker.queue_depth == worker._queue.maxsize


@pytest.mark.asyncio
async def test_existing_decision_is_skipped_on_catchup_reentry(
    catchup_database, market_snapshot, monkeypatch
):
    worker = _worker(catchup_database)
    existing_snapshot = market_snapshot
    decision = worker.strategy.evaluate(
        existing_snapshot,
        risk=calculate_risk_snapshot(
            existing_snapshot,
            max_trade_risk_percent=2,
            max_aggregate_risk_percent=6,
        ),
        candles_are_closed=True,
    )
    ShadowDecisionRepository(catchup_database).persist(decision)
    rows = _inputs(market_snapshot, 2)
    rows[0] = (
        existing_snapshot,
        rows[0][1],
        calculate_risk_snapshot(
            existing_snapshot, max_trade_risk_percent=2, max_aggregate_risk_percent=6
        ),
    )
    last_snapshot, last_id, _risk = rows[-1]
    monkeypatch.setattr(
        shadow_service_module,
        "load_persisted_snapshot_page",
        lambda *_args, **_kwargs: PersistedSnapshotPage(
            tuple(rows), SnapshotCursor(last_snapshot.generated_at, str(last_id)), False, 2
        ),
    )

    await worker._catch_up()
    assert worker.queue_depth == 1
    queued = worker._queue.get_nowait()
    assert worker._key(queued) != worker._key(ShadowInput(existing_snapshot, None, None, True))
    assert worker._key(ShadowInput(existing_snapshot, None, None, True)) in worker._completed_keys


@pytest.mark.asyncio
async def test_failed_catchup_input_remains_retryable_without_cursor_rewind(
    catchup_database, market_snapshot, monkeypatch
):
    worker = _worker(catchup_database)
    snapshot = _with_offset(market_snapshot, 1)
    item = ShadowInput(snapshot, uuid4(), None, True)
    key = worker._key(item)
    worker._needs_catchup = False

    def fail_evaluation(*_args, **_kwargs):
        raise RuntimeError("test-only")

    monkeypatch.setattr(worker.strategy, "evaluate", fail_evaluation)
    await worker._process(item)
    assert worker._catchup_pending_keys == {key}
    assert worker._catchup_pending[0] == (key, item)
    assert worker._needs_catchup is True


def test_page_query_materialization_limit_is_explicit(monkeypatch):
    from services import shadow_replay

    statements = []

    class Result:
        def all(self):
            return []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query):
            statements.append(query)
            return Result()

    class DatabaseStub:
        def session(self):
            return Session()

    monkeypatch.setattr(shadow_replay, "_reconstruct_snapshot_rows", lambda *_args: [])
    page = load_persisted_snapshot_page(DatabaseStub(), limit=13)
    assert page.rows_examined == 0
    assert statements[0]._limit_clause.value == 14  # one bounded look-ahead row


def test_catchup_no_longer_calls_unbounded_history_loader():
    import inspect

    source = inspect.getsource(ShadowDecisionWorker._catch_up)
    assert "limit=None" not in source
    assert "load_persisted_snapshots" not in source
