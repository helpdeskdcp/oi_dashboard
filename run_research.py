"""
run_research.py -- CLI to run hypothesis-tests against REAL historical data.

Demonstrates the research_framework.py pipeline end-to-end with ONE example
hypothesis. This is a STARTING POINT, not a finished product -- add new
Hypothesis objects below to test your own ideas the same way.

USAGE:
    ./venv/bin/python3 run_research.py --symbol NIFTY --from 2026-07-01 --to 2026-07-28
"""
import argparse
import sqlite3
from research_framework import Hypothesis, run_hypothesis_test, log_result

DB_PATH = "oi_history.db"


def load_cycles_with_strikes(symbol, date_from, date_to):
    """Loads cycles + their ATM-strike's OI data for the hypothesis-testing pipeline."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cycles = conn.execute(
        "SELECT * FROM cycles WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY ts ASC",
        (symbol, date_from, date_to),
    ).fetchall()
    result = []
    for c in cycles:
        strikes = conn.execute("SELECT * FROM strikes WHERE cycle_id=?", (c["id"],)).fetchall()
        atm_row = next((s for s in strikes if s["strike"] == c["atm"]), None)
        total_oi = sum((s["ce_oi"] or 0) + (s["pe_oi"] or 0) for s in strikes)
        atm_oi = ((atm_row["ce_oi"] or 0) + (atm_row["pe_oi"] or 0)) if atm_row else None
        result.append({
            "ts": c["ts"], "underlying_ltp": c["underlying_ltp"],
            "atm_oi": atm_oi, "total_oi_nearby": total_oi if total_oi > 0 else None,
        })
    conn.close()
    return result


# ============================================================
# EXAMPLE HYPOTHESIS #1: OI Concentration Index
# ============================================================
def compute_oi_concentration(window):
    """Ratio of ATM-strike's combined CE+PE OI to total nearby-strike OI.
    EXPERIMENTAL -- untested until run_hypothesis_test() says otherwise."""
    if not window:
        return None
    latest = window[-1]
    total_oi = latest.get("total_oi_nearby")
    atm_oi = latest.get("atm_oi")
    if not total_oi or atm_oi is None:
        return None
    return atm_oi / total_oi


def compute_forward_movements(cycles, lookforward=20):
    """Pre-computes the max forward price-movement (%) for every cycle --
    used both to auto-calibrate a genuinely-discriminating threshold AND
    to evaluate outcomes, so both stay consistent."""
    movements = [None] * len(cycles)
    for i in range(len(cycles) - lookforward):
        current_ltp = cycles[i]["underlying_ltp"]
        if not current_ltp:
            continue
        future_prices = [cycles[j]["underlying_ltp"] for j in range(i + 1, i + 1 + lookforward) if cycles[j]["underlying_ltp"]]
        if not future_prices:
            continue
        movements[i] = max(abs(p - current_ltp) / current_ltp for p in future_prices) * 100
    return movements


def make_outcome_fn(movements, threshold_pct):
    """Returns an outcome_fn closure using a PRE-CALIBRATED threshold."""
    def outcome_fn(cycles, i):
        if i >= len(movements) or movements[i] is None:
            return None
        return movements[i] < threshold_pct
    return outcome_fn


HYPOTHESES = [
    Hypothesis(
        name="oi_concentration_index",
        description="Ratio of ATM-strike combined OI to total nearby-strike OI. Intuition: heavy "
                     "concentration at one strike may indicate dealer/institutional pinning around "
                     "that level, which COULD suppress near-term movement. EXPERIMENTAL.",
        compute_fn=compute_oi_concentration,
        predicts="High concentration (top 20% of observed values) predicts the price stays within "
                 "the auto-calibrated (median) movement-threshold for the next 20 cycles (range-bound).",
    ),
    # Add more Hypothesis objects here to test other ideas the same way.
]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run hypothesis-tests against real historical data")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--from", dest="date_from", type=str, required=True)
    parser.add_argument("--to", dest="date_to", type=str, required=True)
    args = parser.parse_args()

    print(f"Loading cycles for {args.symbol} [{args.date_from} to {args.date_to}]...")
    cycles = load_cycles_with_strikes(args.symbol, args.date_from, args.date_to)
    print(f"Loaded {len(cycles)} cycles.\n")

    if not cycles:
        print("No data found -- check the symbol/date-range.")
        exit(1)

    # Auto-calibrate the outcome-threshold from the data's OWN distribution
    # (median forward-movement) -- guarantees a genuinely ~50% baseline by
    # construction, rather than a manually-guessed percentage that might be
    # trivially-true (or trivially-false) almost all the time.
    movements = compute_forward_movements(cycles, lookforward=20)
    valid_movements = sorted(m for m in movements if m is not None)
    if not valid_movements:
        print("Not enough forward-data to calibrate a threshold.")
        exit(1)
    calibrated_threshold = valid_movements[len(valid_movements) // 2]   # median
    print(f"Auto-calibrated threshold: {calibrated_threshold:.3f}% (median 20-cycle forward-movement across {len(valid_movements)} samples)\n")
    outcome_fn = make_outcome_fn(movements, calibrated_threshold)

    for hyp in HYPOTHESES:
        print(f"=== Testing: {hyp.name} ===")
        print(f"Hypothesis: {hyp.description}")
        print(f"Predicts:   {hyp.predicts}")
        result = run_hypothesis_test(hyp, cycles, threshold_percentile=80, outcome_fn=outcome_fn)
        print(f"\nVerdict: {result.verdict}")
        print(f"Reason:  {result.reason}")
        print(f"Sample size: {result.sample_size}")
        if result.precision is not None:
            print(f"Precision: {result.precision}  Recall: {result.recall}")
            print(f"Baseline hit-rate: {result.baseline_hit_rate}  Hypothesis hit-rate: {result.hypothesis_hit_rate}")
        log_result(hyp, result)
        print(f"\nLogged to research_log.json. Hypothesis status is now: '{hyp.status}'\n")
        print("-" * 70)
