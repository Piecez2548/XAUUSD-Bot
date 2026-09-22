"""Thai presentation helpers for the Telegram operator surface.

This module deliberately contains no domain or health calculation.  It only
maps already-authoritative values into concise operator-facing text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

_BANGKOK = ZoneInfo("Asia/Bangkok")
_THAI_MONTHS = (
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
)

_STATE_TEXT = {
    "CONNECTED": ("🟢", "ปกติ"),
    "HEALTHY": ("🟢", "ปกติ"),
    "RUNNING": ("🟢", "กำลังทำงาน"),
    "ACTIVE": ("🟢", "กำลังทำงาน"),
    "DEGRADED": ("🟡", "ทำงานได้ แต่มีบางส่วนผิดปกติ"),
    "PAUSED": ("🟡", "หยุดชั่วคราว"),
    "INTERRUPTED": ("🟡", "ถูกขัดจังหวะ"),
    "UNKNOWN": ("⚪", "ยังไม่ทราบสถานะ"),
    "ERROR": ("🔴", "เกิดข้อผิดพลาด"),
    "FAILED": ("🔴", "ล้มเหลว"),
    "DISABLED": ("⚫", "ปิดใช้งาน"),
    "STOPPED": ("⚫", "หยุดทำงาน"),
    "DISCONNECTED": ("🔴", "ไม่ได้เชื่อมต่อ"),
    "NOT_READY": ("🟡", "ยังไม่พร้อม"),
}

_TRADE_TEXT = {
    "TP": "✅ TP",
    "SL": "🔴 SL",
    "AMBIGUOUS": "🟡 AMBIGUOUS",
    "EXPIRED": "⚪ EXPIRED",
    "PENDING": "🟡 PENDING",
    "OPEN": "🟢 OPEN",
}

_WORKER_LABELS = {
    "worker:history": "ข้อมูลย้อนหลัง",
    "worker:shadow": "Shadow",
    "worker:shadow_outcome": "ประเมินผล Shadow",
    "worker:forward_shadow": "Forward Shadow",
}


def translate_health_state(state: object, *, technical: bool = True) -> str:
    """Render a canonical health state without changing the canonical value."""

    normalized = str(state or "UNKNOWN").upper()
    emoji, text = _STATE_TEXT.get(normalized, ("⚪", "ยังไม่ทราบสถานะ"))
    return f"{emoji} {text}" + (f" ({normalized})" if technical else "")


def translate_trade_state(state: object) -> str:
    normalized = str(state or "UNKNOWN").upper()
    return _TRADE_TEXT.get(normalized, normalized)


def translate_worker_name(component: object) -> str:
    value = str(component or "")
    return _WORKER_LABELS.get(value, value.replace("_", " ").title())


def format_thai_datetime(value: Any, *, include_utc: bool = False) -> str:
    """Display an authoritative UTC timestamp in Asia/Bangkok time."""

    if value is None:
        return "ยังไม่มีข้อมูล"
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if not isinstance(parsed, datetime):
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(_BANGKOK)
    result = f"{local.day:02d} {_THAI_MONTHS[local.month - 1]} {local.year:04d} {local:%H:%M} น."
    if include_utc:
        result += f"\nเวลาอ้างอิง UTC: {parsed.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC"
    return result


def format_value(value: object, digits: int = 2, *, signed: bool = False) -> str:
    if value is None:
        return "ยังไม่มีข้อมูล"
    if isinstance(value, (int, float)):
        return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
    return str(value)


def execution_disabled() -> str:
    return "🔒 การเทรดเงินจริง: ปิดอยู่"


def status_line(label: str, state: object, *, connection: bool = False) -> str:
    normalized = str(state or "UNKNOWN").upper()
    if connection and normalized == "CONNECTED":
        rendered = "🟢 เชื่อมต่อแล้ว (CONNECTED)"
    elif connection and normalized == "RUNNING":
        rendered = "🟢 กำลังทำงาน (RUNNING)"
    else:
        rendered = translate_health_state(normalized)
    return f"{label:<18}{rendered}"


def thai_help() -> str:
    return """📖 คำสั่ง XAUUSD BOT

🚀 ควบคุมระบบ
/start — เริ่มระบบติดตามตลาด
/stop — หยุดระบบติดตามตลาด
/restart — เริ่มระบบใหม่
/status — ดูสถานะการทำงาน
/health — ตรวจสุขภาพทั้งระบบ

📊 ตลาดและบัญชี
/account — ดูสถานะบัญชี MT5
/market — ดูสถานะตลาด
/positions — ดู Position ที่ตรวจพบ
/risk — ดูสถานะความเสี่ยง

🧠 Shadow Strategy
/decision — ดูการตัดสินใจล่าสุด
/shadow — ดูสรุประบบ Shadow
/shadowhealth — ตรวจสุขภาพ Shadow Worker
/outcome — ดูผลลัพธ์ Shadow ล่าสุด
/performance — ดูผลงาน Shadow
/outcomehealth — ตรวจระบบประเมินผล Shadow
/strategy — ดูกฎกลยุทธ์ปัจจุบัน
/strategies — ดูกลยุทธ์ที่มีในระบบ

🧪 Research & Backtest
/research — ดูสถานะระบบวิจัย
/backtests — ดู Backtest ล่าสุด
/backtest — เริ่ม Backtest
/compare — เปรียบเทียบผลกลยุทธ์
/robustness — ดูผลทดสอบความทนทาน
/costs — ดูผลกระทบ Spread/Slippage/Cost
/stability — ดูความเสถียรตามช่วงเวลา

🔭 Forward Validation
/forward — ดูสถานะ Forward Test
/forwardhealth — ตรวจสุขภาพ Forward Worker
/forwardtrades — ดูไม้จำลอง Forward
/forwardperformance — ดูผลงาน Forward

🛠️ อื่น ๆ
/dashboard — เปิด Dashboard
/logs — ดูเหตุการณ์ล่าสุด
/help — แสดงคำสั่งทั้งหมด

🔒 การเทรดเงินจริง: ปิดอยู่"""
