"""Bounded, deterministic, read-only Model Inference V2 dataset builder.

This module is deliberately outside the Live, Forward Shadow, risk, and
execution paths.  It reads persisted Strategy Intelligence and Forward
evidence, emits an auditable research artifact, and never writes application
tables.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from models.model_inference_dataset import (
    DATASET_CONTRACT_VERSION,
    DATASET_V2_CONTRACT_VERSION,
    FEATURE_CONTRACT_VERSION,
    DatasetAuditV1,
    DatasetManifestV1,
    DatasetManifestV2,
    DatasetRowV1,
    DatasetRowV2,
    dataset_row_fingerprint,
)
from persistence.orm import (
    ForwardSignalRecord,
    ForwardTradeRecord,
    ForwardValidationSessionRecord,
    ModelInferenceEvaluationRecord,
    PairZoneEvaluationRecord,
    StrategyIntelligenceRecord,
)
from services.model_inference import INFERENCE_VERSION

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
BUILDER_SEMANTICS_VERSION = "model_inference_dataset_builder_v1"
BUILDER_V2_SEMANTICS_VERSION = "model_inference_dataset_builder_v2"
PROVENANCE_CONTRACT_VERSION = "strategy_intelligence_provenance_v2"
COMPLETED_STATES = frozenset({"TP", "SL", "AMBIGUOUS", "EXPIRED"})
TRAINABLE_LABELS = frozenset({"TP", "SL"})

# This is the complete V1 feature whitelist.  It is intentionally a small
# market-context contract, rather than a copy of context_json/evidence_json.
FEATURE_KEYS = (
    "data_status",
    "m15_trend",
    "m5_trend",
    "m15_structure",
    "m5_structure",
    "m15_atr",
    "m5_atr",
    "m15_displacement",
    "m5_displacement",
    "m5_relative_volatility",
    "session",
    "m15_swing_high_count",
    "m15_swing_low_count",
    "m5_swing_high_count",
    "m5_swing_low_count",
)
REQUIRED_FEATURE_KEYS = frozenset(
    {
        "data_status",
        "m15_trend",
        "m5_trend",
        "m15_structure",
        "m5_structure",
        "m15_atr",
        "m5_atr",
        "m15_displacement",
        "m5_displacement",
        "m5_relative_volatility",
        "session",
        "m15_swing_high_count",
        "m15_swing_low_count",
        "m5_swing_high_count",
        "m5_swing_low_count",
    }
)
TIMESTAMP_KEYS = frozenset(
    {
        "as_of",
        "timestamp",
        "created_at",
        "evaluated_at",
        "terminal_timestamp",
        "pair_first_timestamp",
        "pair_second_timestamp",
        "latest_m5_timestamp",
        "latest_m15_timestamp",
        "pivot_timestamp",
        "confirmed_at",
    }
)


class DatasetArtifactDependencyError(RuntimeError):
    """Raised when an explicitly requested optional artifact format is absent."""


class DatasetReadOnlyViolation(RuntimeError):
    """Raised if a dataset source attempts a non-read SQL statement."""


class _Cursor:
    def __init__(self, timestamp: datetime, row_id: str) -> None:
        self.timestamp = timestamp
        self.row_id = row_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("dataset timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_stable(value).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _payload_timestamps(value: Any, path: str = "") -> Iterator[tuple[str, datetime]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if str(key) in TIMESTAMP_KEYS:
                if item is None:
                    continue
                timestamp = _parse_timestamp(item)
                if timestamp is None:
                    raise ValueError(f"malformed source timestamp: {child_path}")
                yield child_path, timestamp
            yield from _payload_timestamps(item, child_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _payload_timestamps(item, f"{path}[{index}]")


@contextmanager
def _read_only_session(database: Any) -> Iterator[Session]:
    """Open a query-only transaction without calling Database.session()."""

    connection = database.engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA query_only=ON")
        yield session
        transaction.rollback()
    except Exception:
        transaction.rollback()
        raise
    finally:
        if connection.dialect.name == "sqlite":
            # query_only is connection-scoped; never leak it through the
            # Database connection pool into an unrelated application session.
            connection.exec_driver_sql("PRAGMA query_only=OFF")
        session.close()
        connection.close()


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _counts(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


class ModelInferenceDatasetBuilder:
    """Build rows by keyset pages; no application-table writes are possible."""

    def __init__(
        self,
        database: Any,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        session_aware_v2_accepted: bool = False,
        dataset_contract_version: str = DATASET_CONTRACT_VERSION,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        self.database = database
        self.page_size = page_size
        self.session_aware_v2_accepted = session_aware_v2_accepted
        if dataset_contract_version not in {DATASET_CONTRACT_VERSION, DATASET_V2_CONTRACT_VERSION}:
            raise ValueError("unsupported dataset contract version")
        self.dataset_contract_version = dataset_contract_version

    @property
    def _is_v2(self) -> bool:
        return self.dataset_contract_version == DATASET_V2_CONTRACT_VERSION

    def _page(self, cursor: _Cursor | None) -> list[tuple[Any, ...]]:
        statement = (
            select(
                StrategyIntelligenceRecord,
                ForwardValidationSessionRecord,
                ForwardSignalRecord,
                ForwardTradeRecord,
                PairZoneEvaluationRecord,
                ModelInferenceEvaluationRecord,
            )
            .outerjoin(
                ForwardValidationSessionRecord,
                ForwardValidationSessionRecord.id == StrategyIntelligenceRecord.forward_session_id,
            )
            .outerjoin(
                ForwardSignalRecord,
                ForwardSignalRecord.id == StrategyIntelligenceRecord.forward_signal_id,
            )
            .outerjoin(
                ForwardTradeRecord,
                ForwardTradeRecord.id == StrategyIntelligenceRecord.forward_trade_id,
            )
            .outerjoin(
                PairZoneEvaluationRecord,
                PairZoneEvaluationRecord.forward_session_id
                == StrategyIntelligenceRecord.forward_session_id,
            )
            .outerjoin(
                ModelInferenceEvaluationRecord,
                and_(
                    ModelInferenceEvaluationRecord.source_intelligence_record_id
                    == StrategyIntelligenceRecord.id,
                    ModelInferenceEvaluationRecord.inference_version == INFERENCE_VERSION,
                ),
            )
            .order_by(StrategyIntelligenceRecord.detected_at, StrategyIntelligenceRecord.id)
            .limit(self.page_size + 1)
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    StrategyIntelligenceRecord.detected_at > cursor.timestamp,
                    and_(
                        StrategyIntelligenceRecord.detected_at == cursor.timestamp,
                        StrategyIntelligenceRecord.id > cursor.row_id,
                    ),
                )
            )
        with _read_only_session(self.database) as session:
            return list(session.execute(statement).all())

    def iter_rows(self) -> Iterator[DatasetRowV1 | DatasetRowV2]:
        cursor: _Cursor | None = None
        while True:
            page = self._page(cursor)
            has_more = len(page) > self.page_size
            page_seen: set[str] = set()
            for source, forward_session, signal, trade, pair_zone, evaluation in page[
                : self.page_size
            ]:
                try:
                    row = self._row(
                        source, forward_session, signal, trade, pair_zone, evaluation,
                        duplicate=source.candidate_id in page_seen,
                    )
                except Exception:
                    row = self._unresolved_row(source, "MALFORMED_SOURCE_EVIDENCE")
                page_seen.add(source.candidate_id)
                yield row
            if not has_more or not page:
                return
            source = page[self.page_size - 1][0]
            cursor = _Cursor(_utc(source.detected_at), source.id)

    def _row(
        self,
        source: StrategyIntelligenceRecord,
        forward_session: ForwardValidationSessionRecord | None,
        signal: ForwardSignalRecord | None,
        trade: ForwardTradeRecord | None,
        pair_zone: PairZoneEvaluationRecord | None,
        evaluation: ModelInferenceEvaluationRecord | None,
        *,
        duplicate: bool,
    ) -> DatasetRowV1 | DatasetRowV2:
        if self._is_v2 and source.provenance_contract_version != PROVENANCE_CONTRACT_VERSION:
            return self._unresolved_row(
                source, "LEGACY_PROVENANCE_CONTRACT", legacy_contract=True
            )
        cutoff = self._causal_cutoff(source, signal)
        if cutoff is None:
            return self._unresolved_row(source, "MISSING_DECISION_BOUNDARY")
        reasons: list[str] = []
        causal_violation = False
        classification = self._classification(evaluation, source, reasons)
        context = source.context_json if isinstance(source.context_json, dict) else {}
        evidence = source.evidence_json if isinstance(source.evidence_json, list) else []
        context_as_of = _parse_timestamp(context.get("as_of"))
        context_latest_m5 = _parse_timestamp(context.get("latest_m5_timestamp"))
        context_latest_m15 = _parse_timestamp(context.get("latest_m15_timestamp"))
        source_timestamps: dict[str, datetime | None] = {
            "candidate_detected_at": _utc(source.detected_at),
            "context_as_of": context_as_of,
            "context_latest_m5_timestamp": context_latest_m5,
            "context_latest_m15_timestamp": context_latest_m15,
            "session_started_at": (
                None if forward_session is None else _utc(forward_session.started_at)
            ),
            "signal_timestamp": None if signal is None else _utc(signal.timestamp),
            "signal_pair_first_timestamp": (
                None if signal is None or signal.pair_first_timestamp is None
                else _utc(signal.pair_first_timestamp)
            ),
            "signal_pair_second_timestamp": (
                None if signal is None or signal.pair_second_timestamp is None
                else _utc(signal.pair_second_timestamp)
            ),
            "trade_timestamp": None if trade is None else _utc(trade.timestamp),
        }
        if not self._is_v2:
            source_timestamps["signal_created_at"] = (
                None if signal is None else _utc(signal.created_at)
            )
            source_timestamps["pair_zone_evaluation_at"] = (
                None if pair_zone is None else _utc(pair_zone.evaluation_at)
            )
        if self._is_v2:
            source_timestamps.update(
                {
                    "observation_available_at": (
                        None if source.observation_available_at is None
                        else _utc(source.observation_available_at)
                    ),
                    "signal_decision_at": (
                        None if signal is None or signal.signal_decision_at is None
                        else _utc(signal.signal_decision_at)
                    ),
                    "decision_available_at": (
                        None if signal is None or signal.decision_available_at is None
                        else _utc(signal.decision_available_at)
                    ),
                    "outcome_available_at": (
                        None if trade is None or trade.outcome_available_at is None
                        else _utc(trade.outcome_available_at)
                    ),
                }
            )
        if duplicate:
            reasons.append("DUPLICATE_CANDIDATE_IDENTITY")
        if source.timeframe != "M15":
            reasons.append("UNSUPPORTED_STRATEGY_TIMEFRAME")
        if self._is_v2 and (
            source.source_timeframe != "M15" or source.confirmation_timeframe != "M5"
        ):
            reasons.append("INVALID_TIMEFRAME_PROVENANCE")
        if self._is_v2 and source.observation_available_at is None:
            reasons.append("MISSING_OBSERVATION_BOUNDARY")
        if source.execution_allowed is not False:
            reasons.append("EXECUTION_AUTHORITY_PRESENT")

        persistence_only = {
            "signal_created_at",
            "pair_zone_evaluation_at",
            # This is the future outcome's availability boundary, not a
            # pre-decision feature timestamp.
            "outcome_available_at",
        }
        for name, timestamp in source_timestamps.items():
            if (
                name != "candidate_detected_at"
                and name not in persistence_only
                and timestamp is not None
                and timestamp > cutoff
            ):
                reasons.append("CAUSAL_TIMESTAMP_AFTER_CUTOFF")
                causal_violation = True
        if (
            signal is not None
            and signal.pair_first_timestamp is not None
            and signal.pair_second_timestamp is not None
            and signal.pair_first_timestamp > signal.pair_second_timestamp
        ):
            reasons.append("UNORDERED_SOURCE_TIMESTAMPS")
            causal_violation = True

        if context.get("symbol") != source.symbol:
            reasons.append("STRATEGY_CONTEXT_SYMBOL_MISMATCH")
        if context_as_of is None or context_latest_m5 is None or context_latest_m15 is None:
            reasons.append("MISSING_CONTEXT_CAUSAL_TIMESTAMP")
        payloads = (context, evidence)
        for payload in payloads:
            for _path, timestamp in _payload_timestamps(payload):
                if timestamp > cutoff:
                    reasons.append("FUTURE_FEATURE_EVIDENCE")
                    causal_violation = True
        if not self._ordered_swing_points(context):
            reasons.append("UNORDERED_SOURCE_TIMESTAMPS")
            causal_violation = True

        if signal is not None:
            for payload in (
                signal.h1_context_json,
                signal.confirmation_candle_json,
                signal.market_observation_json,
            ):
                for _path, timestamp in _payload_timestamps(payload):
                    if timestamp > cutoff:
                        reasons.append("POST_DECISION_SIGNAL_EVIDENCE")
                        causal_violation = True
        if pair_zone is not None and not self._is_v2:
            if pair_zone.evaluation_at > cutoff:
                reasons.append("POST_DECISION_PAIR_ZONE_EVIDENCE")
                causal_violation = True
            for timestamp in (
                pair_zone.evaluated_m5_timestamp,
                pair_zone.evaluated_m15_timestamp,
            ):
                if timestamp is not None and timestamp > cutoff:
                    reasons.append("FUTURE_PAIR_ZONE_CANDLE")
                    causal_violation = True

        features, missing_features = self._features(context)
        if missing_features:
            reasons.append("MISSING_REQUIRED_FEATURE")
        if context.get("data_status") != "READY":
            reasons.append("STRATEGY_CONTEXT_NOT_READY")
        if not self.session_aware_v2_accepted:
            reasons.append("SESSION_AWARE_V2_NOT_ACCEPTED")

        state, label = self._outcome_state(
            source, forward_session, signal, trade, reasons, cutoff, v2=self._is_v2
        )
        if state == "OUTCOME_COMPLETED" and label in TRAINABLE_LABELS:
            eligibility = "TRAINABLE"
            if missing_features or causal_violation or reasons:
                eligibility = "NON_TRAINABLE"
        elif state == "UNRESOLVED":
            eligibility = "UNRESOLVED"
        else:
            eligibility = "NON_TRAINABLE"
        if causal_violation:
            eligibility = "UNRESOLVED"
        if "DUPLICATE_CANDIDATE_IDENTITY" in reasons:
            eligibility = "UNRESOLVED"

        # State, label, and features are deterministic.  Terminal trade fields
        # are deliberately absent from both the feature and provenance payloads.
        provenance = {
            "strategy_intelligence_record_id": source.id,
            "strategy": source.strategy,
            "strategy_version": source.strategy_version,
            "intelligence_version": source.intelligence_version,
            "intelligence_runtime_version": source.intelligence_runtime_version,
            "evidence_version": source.evidence_version,
            "forward_session_id": source.forward_session_id,
            "forward_signal_id": source.forward_signal_id,
            "forward_trade_id": source.forward_trade_id,
            "pair_zone_event_id": source.pair_zone_event_id,
            "pair_zone_decision_evidence_id": source.pair_zone_decision_evidence_id,
            "model_inference_version": None if evaluation is None else evaluation.inference_version,
        }
        if self._is_v2:
            provenance["provenance_contract_version"] = source.provenance_contract_version
        identity_payload = {
            "dataset_contract_version": self.dataset_contract_version,
            "candidate_id": source.candidate_id,
        }
        row_identity = _hash(identity_payload)
        row_payload = {
            "dataset_contract_version": self.dataset_contract_version,
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "candidate_id": source.candidate_id,
            "symbol": source.symbol,
            "candidate_timestamp": _utc(source.detected_at).isoformat(),
            "causal_cutoff_timestamp": cutoff.isoformat(),
            "state": state,
            "training_eligibility": eligibility,
            "reason_codes": tuple(sorted(set(reasons))),
            "classification": classification,
            "outcome_label": label,
            "features": features,
            "source_provenance": provenance,
            "source_timestamps": _json_value(source_timestamps),
            "row_identity": row_identity,
        }
        if self._is_v2:
            row_payload.update(
                {
                    "source_timeframe": source.source_timeframe,
                    "confirmation_timeframe": source.confirmation_timeframe,
                    "provenance_contract_version": source.provenance_contract_version,
                    "observation_available_at": _json_value(source.observation_available_at),
                    "signal_decision_at": source_timestamps["signal_decision_at"],
                    "decision_available_at": source_timestamps["decision_available_at"],
                    "pair_zone_decision_evidence_id": (
                        None
                        if signal is None
                        else signal.pair_zone_decision_evidence_id
                    ),
                }
            )
        row_fingerprint = dataset_row_fingerprint(row_payload)
        row_type = DatasetRowV2 if self._is_v2 else DatasetRowV1
        return row_type(**row_payload, row_fingerprint=row_fingerprint)

    @staticmethod
    def _causal_cutoff(
        source: StrategyIntelligenceRecord,
        signal: ForwardSignalRecord | None,
    ) -> datetime | None:
        if source.provenance_contract_version == PROVENANCE_CONTRACT_VERSION:
            if signal is not None and signal.decision_available_at is not None:
                return _utc(signal.decision_available_at)
            if source.observation_available_at is not None:
                return _utc(source.observation_available_at)
            return None
        return _utc(source.detected_at)

    @staticmethod
    def _classification(
        evaluation: ModelInferenceEvaluationRecord | None,
        source: StrategyIntelligenceRecord,
        reasons: list[str],
    ) -> str | None:
        if evaluation is None:
            return None
        if (
            evaluation.source_candidate_id != source.candidate_id
            or evaluation.source_intelligence_record_id != source.id
            or evaluation.forward_session_id != source.forward_session_id
            or evaluation.forward_signal_id != source.forward_signal_id
            or evaluation.pair_zone_event_id != source.pair_zone_event_id
            or evaluation.execution_allowed is not False
        ):
            reasons.append("MODEL_INFERENCE_PROVENANCE_MISMATCH")
            return None
        if evaluation.advisory_classification not in {
            "SUPPORTIVE",
            "CAUTION",
            "OBSERVATION_ONLY",
        }:
            reasons.append("MODEL_INFERENCE_CLASSIFICATION_INVALID")
            return None
        return evaluation.advisory_classification

    @staticmethod
    def _features(context: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        features: dict[str, Any] = {}
        for key in FEATURE_KEYS:
            if key.endswith("_swing_high_count") or key.endswith("_swing_low_count"):
                timeframe = key.split("_swing_")[0]
                kind = "SWING_HIGH" if "high" in key else "SWING_LOW"
                points = context.get(f"{timeframe}_swing_points")
                if isinstance(points, list):
                    features[key] = sum(
                        1
                        for point in points
                        if isinstance(point, dict) and point.get("kind") == kind
                    )
                continue
            if key in context:
                features[key] = context[key]
        missing = {
            key
            for key in REQUIRED_FEATURE_KEYS
            if key not in features or features[key] is None
        }
        for key in REQUIRED_FEATURE_KEYS & set(features):
            if key not in {
                "data_status",
                "m15_trend",
                "m5_trend",
                "m15_structure",
                "m5_structure",
                "session",
            } and not _finite(features[key]):
                missing.add(key)
        return features, missing

    def _unresolved_row(
        self,
        source: StrategyIntelligenceRecord,
        reason: str,
        *,
        legacy_contract: bool = False,
    ) -> DatasetRowV1 | DatasetRowV2:
        cutoff = _utc(source.detected_at)
        contract_version = (
            DATASET_CONTRACT_VERSION if legacy_contract else self.dataset_contract_version
        )
        provenance = {
            "strategy_intelligence_record_id": source.id,
            "strategy": source.strategy,
            "strategy_version": source.strategy_version,
            "evidence_version": source.evidence_version,
        }
        row_identity = _hash(
            {
                "dataset_contract_version": contract_version,
                "candidate_id": source.candidate_id,
            }
        )
        row_payload = {
            "dataset_contract_version": contract_version,
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "candidate_id": source.candidate_id,
            "symbol": source.symbol,
            "candidate_timestamp": cutoff.isoformat(),
            "causal_cutoff_timestamp": cutoff.isoformat(),
            "state": "UNRESOLVED",
            "training_eligibility": "UNRESOLVED",
            "reason_codes": (reason,),
            "classification": None,
            "outcome_label": "UNRESOLVED",
            "features": {},
            "source_provenance": provenance,
            "source_timestamps": {"candidate_detected_at": cutoff.isoformat()},
            "row_identity": row_identity,
        }
        if self._is_v2 and not legacy_contract:
            row_payload.update(
                {
                    "source_timeframe": source.source_timeframe,
                    "confirmation_timeframe": source.confirmation_timeframe,
                    "provenance_contract_version": source.provenance_contract_version,
                    "observation_available_at": (
                        None if source.observation_available_at is None
                        else _json_value(source.observation_available_at)
                    ),
                    "signal_decision_at": None,
                    "decision_available_at": None,
                    "pair_zone_decision_evidence_id": source.pair_zone_decision_evidence_id,
                }
            )
            return DatasetRowV2(
                **row_payload, row_fingerprint=dataset_row_fingerprint(row_payload)
            )
        return DatasetRowV1(**row_payload, row_fingerprint=dataset_row_fingerprint(row_payload))

    @staticmethod
    def _ordered_swing_points(context: dict[str, Any]) -> bool:
        for key in ("m15_swing_points", "m5_swing_points"):
            points = context.get(key)
            if not isinstance(points, list):
                continue
            timestamps = [
                _parse_timestamp(point.get("pivot_timestamp"))
                for point in points
                if isinstance(point, dict)
            ]
            timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
            if any(left >= right for left, right in zip(timestamps, timestamps[1:], strict=False)):
                return False
        return True

    @staticmethod
    def _outcome_state(
        source: StrategyIntelligenceRecord,
        forward_session: ForwardValidationSessionRecord | None,
        signal: ForwardSignalRecord | None,
        trade: ForwardTradeRecord | None,
        reasons: list[str],
        cutoff: datetime,
        *,
        v2: bool = False,
    ) -> tuple[str, str]:
        if source.forward_signal_id is None and source.forward_trade_id is None:
            return "PRE_SIGNAL_OBSERVATION", "NOT_ELIGIBLE"
        if forward_session is None or signal is None:
            reasons.append("FORWARD_PROVENANCE_MISMATCH")
            return "UNRESOLVED", "UNRESOLVED"
        if signal.timestamp > cutoff:
            reasons.append("SIGNAL_AFTER_CAUSAL_CUTOFF")
            return "UNRESOLVED", "UNRESOLVED"
        signal_valid = (
            source.forward_session_id == signal.session_id
            and source.forward_signal_id == signal.id
            and (
                (
                    source.pair_zone_event_id == signal.zone_id
                    and source.pair_zone_decision_evidence_id is not None
                    and source.pair_zone_decision_evidence_id
                    == signal.pair_zone_decision_evidence_id
                )
                if v2
                else source.pair_zone_event_id == signal.zone_id
            )
            and forward_session.strategy_config_hash == signal.strategy_hash
            and signal.decision in {"BUY", "SELL"}
            and source.direction == signal.decision
            and source.symbol == forward_session.symbol
            and source.strategy == forward_session.strategy_id
            and source.strategy_version == forward_session.strategy_version
            and forward_session.execution_allowed is False
            and signal.execution_allowed is False
        )
        if v2:
            signal_valid = signal_valid and (
                signal.provenance_contract_version == PROVENANCE_CONTRACT_VERSION
                and signal.source_timeframe == "M15"
                and signal.confirmation_timeframe == "M5"
                and signal.signal_decision_at == signal.timestamp
                and signal.decision_available_at is not None
                and signal.decision_available_at > signal.signal_decision_at
                and signal.decision_available_at <= cutoff
                and signal.symbol == forward_session.symbol
            )
        if not signal_valid:
            reasons.append("FORWARD_PROVENANCE_MISMATCH")
            return "UNRESOLVED", "UNRESOLVED"
        if trade is None:
            if source.forward_trade_id is None:
                return "SIGNAL_ELIGIBLE", "SIGNAL_ELIGIBLE"
            reasons.append("FORWARD_PROVENANCE_MISMATCH")
            return "UNRESOLVED", "UNRESOLVED"
        if (
            source.forward_trade_id != trade.id
            or source.forward_session_id != trade.session_id
            or signal.id != trade.signal_id
            or signal.timestamp != trade.timestamp
            or signal.decision != trade.side
            or trade.execution_allowed is not False
        ):
            reasons.append("FORWARD_PROVENANCE_MISMATCH")
            return "UNRESOLVED", "UNRESOLVED"
        if trade.state == "OPEN":
            if any(
                value is not None
                for value in (
                    trade.terminal_timestamp,
                    trade.evaluated_at,
                    trade.gross_r,
                    trade.net_r,
                    trade.mfe_r,
                    trade.mae_r,
                )
            ):
                reasons.append("MALFORMED_OPEN_OUTCOME")
                return "UNRESOLVED", "UNRESOLVED"
            return "OUTCOME_PENDING", "OPEN/PENDING"
        if trade.state not in COMPLETED_STATES:
            reasons.append("UNKNOWN_OUTCOME_STATE")
            return "UNRESOLVED", "UNRESOLVED"
        if (
            trade.terminal_timestamp is None
            or trade.terminal_timestamp <= trade.timestamp
            or trade.outcome_available_at is None
            or trade.outcome_available_at <= trade.terminal_timestamp
            or trade.outcome_available_at <= cutoff
        ):
            reasons.append("INCOMPLETE_OUTCOME_PROVENANCE")
            return "UNRESOLVED", "UNRESOLVED"
        if trade.state in {"TP", "SL"} and (
            not _finite(trade.gross_r) or not _finite(trade.net_r)
        ):
            reasons.append("INCOMPLETE_TERMINAL_LABEL")
            return "UNRESOLVED", "UNRESOLVED"
        if trade.state == "AMBIGUOUS" and (trade.gross_r is not None or trade.net_r is not None):
            reasons.append("AMBIGUOUS_OUTCOME_HAS_RETURN")
            return "UNRESOLVED", "UNRESOLVED"
        return "OUTCOME_COMPLETED", trade.state


class _Audit:
    def __init__(self) -> None:
        self.row_count = 0
        self.inspected_candidate_count = 0
        self.trainable_count = 0
        self.non_trainable_count = 0
        self.unresolved_count = 0
        self.state_counts: Counter[str] = Counter()
        self.label_counts: Counter[str] = Counter()
        self.reason_counts: Counter[str] = Counter()
        self.classification_counts: Counter[str] = Counter()
        self.symbol_counts: Counter[str] = Counter()
        self.contract_version_counts: Counter[str] = Counter()
        self.missing_feature_counts: Counter[str] = Counter()
        self.causal_violation_count = 0
        self.duplicate_identity_count = 0

    def add(self, row: DatasetRowV1) -> None:
        self.row_count += 1
        self.inspected_candidate_count += 1
        self.state_counts[row.state] += 1
        self.label_counts[row.outcome_label] += 1
        self.symbol_counts[row.symbol] += 1
        self.contract_version_counts[row.feature_contract_version] += 1
        if row.training_eligibility == "TRAINABLE":
            self.trainable_count += 1
        elif row.training_eligibility == "UNRESOLVED":
            self.unresolved_count += 1
        else:
            self.non_trainable_count += 1
        for reason in row.reason_codes:
            self.reason_counts[reason] += 1
            if reason in {
                "FUTURE_FEATURE_EVIDENCE",
                "POST_DECISION_SIGNAL_EVIDENCE",
                "POST_DECISION_PAIR_ZONE_EVIDENCE",
                "FUTURE_PAIR_ZONE_CANDLE",
                "CAUSAL_TIMESTAMP_AFTER_CUTOFF",
                "SIGNAL_AFTER_CAUSAL_CUTOFF",
                "UNORDERED_SOURCE_TIMESTAMPS",
            }:
                self.causal_violation_count += 1
            if reason == "DUPLICATE_CANDIDATE_IDENTITY":
                self.duplicate_identity_count += 1
            if reason == "MISSING_REQUIRED_FEATURE":
                for key in FEATURE_KEYS:
                    if key not in row.features:
                        self.missing_feature_counts[key] += 1
        self.classification_counts[row.classification or "UNCLASSIFIED"] += 1

    def model(self) -> DatasetAuditV1:
        return DatasetAuditV1(
            candidate_count=self.row_count,
            inspected_candidate_count=self.inspected_candidate_count,
            builder_exclusion_count=0,
            row_count=self.row_count,
            trainable_count=self.trainable_count,
            non_trainable_count=self.non_trainable_count,
            unresolved_count=self.unresolved_count,
            state_counts=_counts(self.state_counts),
            label_counts=_counts(self.label_counts),
            reason_counts=_counts(self.reason_counts),
            classification_counts=_counts(self.classification_counts),
            symbol_counts=_counts(self.symbol_counts),
            contract_version_counts=_counts(self.contract_version_counts),
            missing_feature_counts=_counts(self.missing_feature_counts),
            causal_violation_count=self.causal_violation_count,
            duplicate_identity_count=self.duplicate_identity_count,
        )


def write_dataset_artifact(
    builder: ModelInferenceDatasetBuilder,
    output_dir: Path,
    *,
    artifact_format: str = "jsonl",
    artifact_name: str = "model_inference_dataset_v1.jsonl",
) -> tuple[DatasetManifestV1 | DatasetManifestV2, Path]:
    """Stream rows into a caller-selected research directory and write a manifest."""

    if artifact_format not in {"jsonl", "parquet"}:
        raise ValueError("artifact_format must be jsonl or parquet")
    if artifact_format == "parquet":
        raise DatasetArtifactDependencyError(
            "Parquet output requires an explicitly installed optional pyarrow dependency; "
            "no dependency was added by this foundation"
        )
    is_v2 = (
        getattr(builder, "dataset_contract_version", DATASET_CONTRACT_VERSION)
        == DATASET_V2_CONTRACT_VERSION
    )
    dataset_contract_version = (
        DATASET_V2_CONTRACT_VERSION if is_v2 else DATASET_CONTRACT_VERSION
    )
    if is_v2 and artifact_name == "model_inference_dataset_v1.jsonl":
        artifact_name = "model_inference_dataset_v2.jsonl"
    manifest_basename = (
        "model_inference_dataset_v2.manifest.json"
        if is_v2
        else "model_inference_dataset_v1.manifest.json"
    )
    artifact_path = output_dir / artifact_name
    manifest_path = output_dir / manifest_basename
    if (
        Path(artifact_name).name != artifact_name
        or Path(artifact_name).is_absolute()
        or artifact_path.exists()
        or manifest_path.exists()
    ):
        raise FileExistsError(
            "artifact name must be a plain new filename and neither artifact nor manifest "
            "may already exist"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    audit = _Audit()
    artifact_temp: str | None = None
    manifest_temp: str | None = None
    artifact_committed = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{artifact_name}.",
            suffix=".partial",
            dir=output_dir,
            delete=False,
        ) as handle:
            artifact_temp = handle.name
            dataset_hasher = hashlib.sha256()
            artifact_hasher = hashlib.sha256()
            semantic_header = {
                "dataset_contract_version": dataset_contract_version,
                "feature_contract_version": FEATURE_CONTRACT_VERSION,
                "builder_semantics_version": (
                    BUILDER_V2_SEMANTICS_VERSION if is_v2 else BUILDER_SEMANTICS_VERSION
                ),
                "bounded_page_size": builder.page_size,
                "session_aware_v2_accepted": builder.session_aware_v2_accepted,
                "artifact_format": artifact_format,
            }
            dataset_hasher.update(_stable(semantic_header).encode("utf-8"))
            dataset_hasher.update(b"\n")
            for row in builder.iter_rows():
                audit.add(row)
                dataset_hasher.update(
                    f"{row.row_identity}:{row.row_fingerprint}\n".encode("ascii")
                )
                line = (_stable(row.model_dump(mode="json")) + "\n").encode("utf-8")
                artifact_hasher.update(line)
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

        audit_model = audit.model()
        manifest_payload = {
            "artifact_contract_version": "model_inference_artifact_v1",
            "dataset_contract_version": dataset_contract_version,
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "artifact_format": artifact_format,
            "artifact_name": artifact_name,
            "dataset_hash": dataset_hasher.hexdigest(),
            "artifact_sha256": artifact_hasher.hexdigest(),
            "row_count": audit_model.row_count,
            "audit": audit_model.model_dump(mode="json"),
            "causal_cutoff_rule": (
                "signal.decision_available_at or observation_available_at in UTC; "
                "labels may occur later but never enter features"
                if is_v2
                else "candidate.detected_at in UTC; labels may occur later but never enter features"
            ),
            "bounded_page_size": builder.page_size,
            "session_aware_v2_accepted": builder.session_aware_v2_accepted,
        }
        manifest_fingerprint = _hash(manifest_payload)
        manifest_type = DatasetManifestV2 if is_v2 else DatasetManifestV1
        manifest = manifest_type(**manifest_payload, manifest_fingerprint=manifest_fingerprint)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{manifest_basename}.",
            suffix=".partial",
            dir=output_dir,
            delete=False,
        ) as handle:
            manifest_temp = handle.name
            handle.write((_stable(manifest.model_dump(mode="json")) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

        if artifact_path.exists() or manifest_path.exists():
            raise FileExistsError("artifact or manifest appeared during build")
        os.rename(artifact_temp, artifact_path)
        artifact_temp = None
        artifact_committed = True
        os.rename(manifest_temp, manifest_path)
        manifest_temp = None
        return manifest, artifact_path
    except Exception:
        if artifact_committed and artifact_path.exists() and not manifest_path.exists():
            artifact_path.unlink()
        raise
    finally:
        for temporary_path in (artifact_temp, manifest_temp):
            if temporary_path is not None:
                with suppress(OSError):
                    Path(temporary_path).unlink(missing_ok=True)
