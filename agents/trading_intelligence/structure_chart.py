"""
agents/trading_intelligence/structure_chart.py -- Milestone 20, Phase 3:
renders one JPEG per structure alert (a close-price line chart, not full
OHLC candlesticks -- no candlestick-charting library was requested or
already present; a line chart with the same overlay markup satisfies
the same "see the levels/zones at a glance" goal without a second new
dependency beyond matplotlib). Best-effort only: never raises, returns
None on any failure (missing candle data, a write error, etc.) --
telegram_notifier's own caller falls back to a text-only send exactly
as specified.

STRICTLY visualization -- this module reads candles/levels/overlay
numbers already computed elsewhere and draws them; it never computes a
new level, never decides a state, never opens a trade. Matches every
other "read-only, additive" module this milestone has added.
"""
import datetime as dt
import logging
import os

import matplotlib
matplotlib.use("Agg")  # headless -- no display on a server, ever
import matplotlib.pyplot as plt
import matplotlib.patches as patches

log = logging.getLogger("oi_dashboard.trading_intelligence.structure_chart")

# Relative to the repo root (app.py's own default Flask static_folder,
# confirmed via Flask(__name__) with no override) -- files written here
# are served at /static/structure_charts/<filename>, exactly the save
# path requested.
CHART_DIR = os.path.join("static", "structure_charts")
MAX_CANDLES_SHOWN = 40


def render_structure_chart(symbol: str, candles: list, *, level: float, state: str | None = None,
                            reversal: dict | None = None, overlay: dict | None = None, confidence: int | None = None,
                            reversal_support: float | None = None, reversal_resistance: float | None = None) -> str | None:
    """Renders one JPEG to CHART_DIR/{symbol}_{timestamp}.jpg. Returns
    the file path on success, None on any failure -- never raises, so a
    charting bug can never break the real alert send (the caller sends
    the text alert either way). `reversal` is optional -- alertable
    states that aren't a confirmed role flip (BREAKOUT_WATCH/
    REVERSAL_RISK) still get a chart, just without the breakout-arrow/
    retest-zone markup that needs a real reversal's candle references."""
    try:
        if not candles:
            return None
        os.makedirs(CHART_DIR, exist_ok=True)
        reversal = reversal or {}

        recent = candles[-MAX_CANDLES_SHOWN:] if len(candles) > MAX_CANDLES_SHOWN else candles
        closes = [c["close"] for c in recent]
        x = list(range(len(closes)))

        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
        ax.plot(x, closes, color="#2563eb", linewidth=1.6, zorder=3)

        is_bullish = reversal.get("current_role") == "SUPPORT"
        support_level = level if is_bullish else reversal_support
        resistance_level = reversal_resistance if is_bullish else level
        if support_level is not None:
            ax.axhline(support_level, color="#16a34a", linestyle="--", linewidth=1.2, zorder=2,
                       label=f"Support {support_level:g}")
        if resistance_level is not None:
            ax.axhline(resistance_level, color="#dc2626", linestyle="--", linewidth=1.2, zorder=2,
                       label=f"Resistance {resistance_level:g}")

        # Retest zone -- an orange rectangle spanning the retest candle's
        # own high/low, near the right edge where the pattern completed.
        retest = reversal.get("retest_candle")
        if retest:
            zone_x = max(0, len(x) - 5)
            rect = patches.Rectangle((zone_x, retest["low"]), 4, max(retest["high"] - retest["low"], 0.01),
                                      linewidth=1.2, edgecolor="#f59e0b", facecolor="#f59e0b", alpha=0.25, zorder=2)
            ax.add_patch(rect)

        # Breakout/breakdown direction arrow.
        breakout = reversal.get("breakout_candle")
        if breakout:
            arrow_x = max(0, len(x) - 8)
            arrow_y0, arrow_y1 = (breakout["low"], breakout["high"]) if is_bullish else (breakout["high"], breakout["low"])
            ax.annotate("", xy=(arrow_x, arrow_y1), xytext=(arrow_x, arrow_y0),
                        arrowprops={"arrowstyle": "-|>", "color": "#1d4ed8", "linewidth": 2}, zorder=4)

        if overlay:
            ax.axhline(overlay["sl"], color="#b91c1c", linewidth=1.0, linestyle=":", zorder=2)
            ax.text(len(x) - 1, overlay["sl"], f" SL {overlay['sl']:g}", color="#b91c1c",
                    va="center", fontsize=8, fontweight="bold")
            for label, value in (("T1", overlay["t1"]), ("T2", overlay["t2"])):
                ax.axhline(value, color="#15803d", linewidth=1.0, linestyle=":", zorder=2)
                ax.text(len(x) - 1, value, f" {label} {value:g}", color="#15803d",
                        va="center", fontsize=8, fontweight="bold")

        if confidence is not None:
            grade = "HIGH" if confidence >= 90 else "STRONG" if confidence >= 80 else "MODERATE" if confidence >= 70 else "WEAK"
            ax.text(0.98, 0.97, f"{confidence}% {grade}", transform=ax.transAxes, ha="right", va="top",
                    fontsize=9, fontweight="bold", color="white",
                    bbox={"boxstyle": "round,pad=0.35", "facecolor": "#111827", "alpha": 0.85})

        if reversal.get("previous_role") and reversal.get("current_role"):
            title = f"{symbol} — {reversal['previous_role']} → {reversal['current_role']}"
        else:
            title = f"{symbol} — {state or 'STRUCTURE UPDATE'} @ {level:g}"
        ax.set_title(title, fontsize=11)
        ax.set_xticks([])
        ax.legend(loc="lower left", fontsize=8, framealpha=0.8)
        fig.tight_layout()

        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{symbol}_{timestamp}.jpg"
        filepath = os.path.join(CHART_DIR, filename)
        fig.savefig(filepath, format="jpg")
        plt.close(fig)
        return filepath
    except Exception as e:
        log.warning(f"Structure chart rendering failed for {symbol} (non-fatal, text alert still sent): {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None
