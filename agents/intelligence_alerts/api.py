"""
agents/intelligence_alerts/api.py -- Milestone 14, Phase 1: read-only
aggregation functions backing the three GET-only
/api/intelligence/alerts/* routes in app.py. Every function here only
reads (store.py's own SELECT-only helpers, or agents.config's fixed
threshold constants) -- nothing in this module writes anything, matching
"no POST/PUT/PATCH/DELETE endpoint in this phase."

Telegram/email "configured" below means the required env vars are
present -- NOT that a real send has succeeded. Checked directly via
os.getenv rather than importing app.py, which this package deliberately
never imports (see intelligence_alerts_cli.py's own docstring for why).
"""
import os

from agents import config as agents_config

from . import store


def get_status() -> dict:
    return {
        "mode": "intelligence_alerts",
        "read_only": True,
        "no_orders_placed": True,
        "alert_count": store.count_total(),
        "last_alert_ts": store.last_alert_ts(),
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "email_configured": bool(agents_config.INTELLIGENCE_ALERT_EMAIL_TO),
    }


def get_recent_page(*, symbol: str | None = None, limit: int = 10, offset: int = 0) -> dict:
    return {
        "items": store.list_recent(symbol=symbol, limit=limit, offset=offset),
        "total": store.count_total(symbol=symbol),
        "limit": limit,
        "offset": offset,
    }


def get_rules() -> dict:
    """Read-only dump of the active threshold config -- lets the
    dashboard show what WOULD trigger an alert, with no write surface to
    change it (thresholds are edited by hand in agents/config.py, same
    convention as every other threshold constant in this codebase)."""
    return {
        "bias_flip": "alerts when the two most recent logged snapshots for a symbol have different bias",
        "confidence_unstable": {
            "window": agents_config.INTELLIGENCE_ALERT_CONFIDENCE_WINDOW,
            "stdev_threshold": agents_config.INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD,
        },
        "greeks_incoherent": "alerts when the latest snapshot's bias and greeks_alignment disagree",
        "oi_non_responsive": {
            "window": agents_config.INTELLIGENCE_ALERT_OI_WINDOW,
        },
    }
