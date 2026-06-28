"""
utils/time_utils.py — Timezone, session, and timestamp utilities.
All times displayed in Eastern Time throughout the bot.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

ET = ZoneInfo("US/Eastern")
UTC = ZoneInfo("UTC")


def now_et() -> datetime:
    """Current datetime in Eastern Time."""
    return datetime.now(ET)


def now_utc() -> datetime:
    """Current datetime in UTC."""
    return datetime.now(UTC)


def to_et(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to Eastern Time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def fmt_et(dt: Optional[datetime] = None, fmt: str = "%Y-%m-%d %H:%M:%S ET") -> str:
    """Format a datetime as Eastern Time string. Defaults to now."""
    if dt is None:
        dt = now_et()
    return to_et(dt).strftime(fmt)


def fmt_et_short(dt: Optional[datetime] = None) -> str:
    """Short format: 09:47:32 ET"""
    return fmt_et(dt, fmt="%H:%M:%S ET")


def fmt_et_full(dt: Optional[datetime] = None) -> str:
    """Full format with date: 2025-01-15 09:47:32 ET"""
    return fmt_et(dt, fmt="%Y-%m-%d %H:%M:%S ET")


def minutes_since(dt: datetime) -> float:
    """Minutes elapsed since a given datetime."""
    return (now_utc() - to_utc(dt)).total_seconds() / 60.0


def seconds_since(dt: datetime) -> float:
    """Seconds elapsed since a given datetime."""
    return (now_utc() - to_utc(dt)).total_seconds()


def to_utc(dt: datetime) -> datetime:
    """Convert any datetime to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_within_minutes(dt: datetime, minutes: float) -> bool:
    """True if dt is within the last N minutes."""
    return seconds_since(dt) <= (minutes * 60)


# ─── SESSION WINDOWS ──────────────────────────────────────────────────────────

SESSION_WINDOWS = {
    "asian":   (time(20, 0),  time(0, 0)),   # 8 PM – midnight ET (previous day logic handled)
    "london":  (time(3, 0),   time(8, 30)),  # 3 AM – 8:30 AM ET
    "ny":      (time(9, 30),  time(16, 0)),  # 9:30 AM – 4:00 PM ET
    "overlap": (time(8, 0),   time(12, 0)),  # London/NY overlap — highest quality
    "crypto_prime": (time(8, 0), time(17, 0)),  # Best BTC institutional hours
}

SESSION_QUALITY = {
    "overlap":      1.0,   # Best
    "ny":           0.85,
    "crypto_prime": 0.80,
    "london":       0.75,
    "asian":        0.60,
    "off_hours":    0.40,  # Still trade (24/7 mode), but lower quality signal bar
}


def current_session() -> Tuple[str, float]:
    """
    Returns the current trading session name and quality score.
    Quality score used by signal_validator to raise/lower confluence bar.
    """
    now = now_et().time()

    # Overlap is highest priority — check first
    if time(8, 0) <= now < time(12, 0):
        return "overlap", SESSION_QUALITY["overlap"]

    if time(9, 30) <= now < time(16, 0):
        return "ny", SESSION_QUALITY["ny"]

    if time(3, 0) <= now < time(8, 30):
        return "london", SESSION_QUALITY["london"]

    if time(8, 0) <= now < time(17, 0):
        return "crypto_prime", SESSION_QUALITY["crypto_prime"]

    if now >= time(20, 0) or now < time(0, 30):
        return "asian", SESSION_QUALITY["asian"]

    return "off_hours", SESSION_QUALITY["off_hours"]


def session_quality_score() -> float:
    """Just the quality score for the current session."""
    _, quality = current_session()
    return quality


def is_blackout(blackout_windows: list) -> bool:
    """
    Check if current ET time falls inside any configured blackout window.
    blackout_windows: list of ("HH:MM", "HH:MM") tuples in ET.
    """
    if not blackout_windows:
        return False
    now = now_et().time()
    for start_str, end_str in blackout_windows:
        h1, m1 = map(int, start_str.split(":"))
        h2, m2 = map(int, end_str.split(":"))
        start = time(h1, m1)
        end   = time(h2, m2)
        if start <= end:
            if start <= now < end:
                return True
        else:  # crosses midnight
            if now >= start or now < end:
                return True
    return False


def get_day_boundary_utc() -> Tuple[datetime, datetime]:
    """Return today's midnight-to-midnight in UTC for daily P&L resets."""
    today_et = now_et().date()
    start_et = datetime(today_et.year, today_et.month, today_et.day,
                        0, 0, 0, tzinfo=ET)
    end_et   = start_et + timedelta(days=1)
    return to_utc(start_et), to_utc(end_et)


def ts_for_db() -> str:
    """ISO 8601 timestamp string for database storage (UTC)."""
    return now_utc().isoformat()
