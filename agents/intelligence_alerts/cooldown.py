"""
agents/intelligence_alerts/cooldown.py -- Milestone 15, Phase 1: Alert
Deduplication & Cooldown Protection. Pure, side-effect-free fingerprint
and confidence-bucket logic -- no I/O, no database, nothing here reads
or writes anything. dedup_store.py owns persistence and the actual
suppress/allow decision; this module only computes values from inputs.

No strike component in the fingerprint: nothing in this codebase's
intelligence-alert data carries an option strike.
MarketIntelligenceSnapshot (intelligence_models.py) has no strike field
at all -- alerts here are about symbol-level bias/confidence/Greeks
conditions, not individual strikes. Dropped rather than fabricated.
"""


def confidence_bucket(confidence) -> str:
    """Widened confidence bucket used in the dedup fingerprint -- a
    3-point wobble (61 -> 63) must not look like a new condition, but a
    genuine jump (61 -> 74) should. Matches the Phase 1 spec's own
    examples (60-69, 70-79, 80+) exactly, generalized below 60 as the
    same width-10 pattern (0-9, 10-19, ...)."""
    confidence = int(confidence or 0)
    if confidence >= 80:
        return "80+"
    floor = (confidence // 10) * 10
    return f"{floor}-{floor + 9}"


def bucket_rank(bucket: str) -> int:
    """Numeric ordering for buckets so a caller can detect a genuine
    INCREASE (strictly higher rank), not just "a different bucket" --
    required by the spec's own bypass rule: a bucket increase bypasses
    suppression, but a same-or-lower bucket does not."""
    if bucket == "80+":
        return 8
    return int(bucket.split("-")[0]) // 10


def make_fingerprint(*, symbol: str, bias: str, confidence, rule: str) -> str:
    """Deterministic identity for "the same market condition" -- same
    inputs always produce the same string. Used for structured logging
    (ALERT_SENT/ALERT_SUPPRESSED_DUPLICATE) and as dedup_store.py's
    audit-friendly display value; dedup_store.py's own persisted lookup
    key is coarser than this (symbol|bias|rule, no bucket) so it can
    implement the bucket-increase ratchet -- see that module's own
    docstring for why."""
    return f"{symbol}|{bias}|{confidence_bucket(confidence)}|{rule}"
