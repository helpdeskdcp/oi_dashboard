"""
agents/trading_intelligence/dual_probability_calibration.py -- two-stage
calibrated probability model: a raw logistic score (fit by gradient
descent) recalibrated by isotonic regression (Pool-Adjacent-Violators
Algorithm, PAVA). numpy-only -- confirmed via requirements.txt and a
repo-wide grep that scipy/scikit-learn are NOT dependencies of this
codebase (only pandas>=2.0.0, which pulls numpy in transitively), so this
avoids adding a new heavy dependency for what PAVA implements in ~20
lines.

Never hardcode a displayed probability -- every output here is either a
genuinely fit calibration curve's value, or an honest None when there
isn't enough data to trust one (see CalibrationResult.calibration_valid).

WALK-FORWARD ONLY: fit()/calibrate_and_score() take pre-split
train/calibration/validation arrays. This module does not itself split
data -- walk_forward_split() is provided as a plain chronological helper
(never a random shuffle) for callers building the split, keeping the
"never randomly shuffle time-series observations" requirement visible
at the call site rather than hidden inside this module.
"""
import dataclasses

import numpy as np


def sigmoid(x: np.ndarray | float) -> np.ndarray | float:
    """Numerically stable logistic sigmoid -- clips the exponent so
    extreme raw scores never overflow (np.exp(-x) for |x| > ~700 would
    otherwise raise a RuntimeWarning / produce inf)."""
    x = np.clip(x, -500.0, 500.0)
    return 1.0 / (1.0 + np.exp(-x))


def logit(p: np.ndarray | float, *, eps: float = 1e-6) -> np.ndarray | float:
    """Inverse of sigmoid, clipped away from the 0/1 boundary to avoid
    log(0)."""
    p = np.clip(p, eps, 1.0 - eps)
    return np.log(p / (1.0 - p))


def walk_forward_split(n: int, *, train_frac: float = 0.6, calib_frac: float = 0.2):
    """Chronological split of `n` time-ordered observations into
    (train_slice, calib_slice, valid_slice) -- NEVER shuffles. The
    remaining `1 - train_frac - calib_frac` goes to validation."""
    if not (0 < train_frac < 1) or not (0 < calib_frac < 1) or train_frac + calib_frac >= 1:
        raise ValueError("train_frac and calib_frac must be positive and sum to less than 1")
    train_end = int(n * train_frac)
    calib_end = int(n * (train_frac + calib_frac))
    return slice(0, train_end), slice(train_end, calib_end), slice(calib_end, n)


class LogisticModel:
    """Plain logistic regression fit by batch gradient descent with L2
    regularization -- the "raw score" stage. Deliberately simple (no
    line search, no Newton steps) since this repo has no scipy/sklearn
    to lean on; `epochs`/`lr` are tuned generously (many epochs, modest
    learning rate) rather than cleverly, which is fine at this dataset
    size (thousands, not millions, of rows)."""

    def __init__(self, n_features: int, *, l2: float = 0.01):
        self.coef_ = np.zeros(n_features)
        self.intercept_ = 0.0
        self.l2 = l2

    def raw_score(self, X: np.ndarray) -> np.ndarray:
        return X @ self.coef_ + self.intercept_

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return sigmoid(self.raw_score(X))

    def fit(self, X: np.ndarray, y: np.ndarray, *, lr: float = 0.1, epochs: int = 800) -> "LogisticModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        n = len(y)
        if n == 0:
            return self
        for _ in range(epochs):
            p = self.predict_proba(X)
            grad_w = X.T @ (p - y) / n + self.l2 * self.coef_
            grad_b = float(np.mean(p - y))
            self.coef_ = self.coef_ - lr * grad_w
            self.intercept_ = self.intercept_ - lr * grad_b
        return self


def _pava(y_sorted: np.ndarray) -> np.ndarray:
    """Pool-Adjacent-Violators: given y already ordered by ascending x,
    returns the monotonically non-decreasing step function minimizing
    sum((y_hat - y)^2) subject to y_hat being non-decreasing. Classic
    stack-based O(n) implementation -- each block is (mean_value,
    weight, count); adjacent blocks are merged whenever the later
    block's mean would be lower than the earlier one's (a "violation")."""
    block_val: list[float] = []
    block_w: list[float] = []
    block_count: list[int] = []
    for yi in y_sorted:
        block_val.append(float(yi))
        block_w.append(1.0)
        block_count.append(1)
        while len(block_val) > 1 and block_val[-2] > block_val[-1]:
            v2, w2, c2 = block_val.pop(), block_w.pop(), block_count.pop()
            v1, w1, c1 = block_val.pop(), block_w.pop(), block_count.pop()
            merged = (v1 * w1 + v2 * w2) / (w1 + w2)
            block_val.append(merged)
            block_w.append(w1 + w2)
            block_count.append(c1 + c2)
    result = np.empty(len(y_sorted), dtype=float)
    pos = 0
    for v, c in zip(block_val, block_count):
        result[pos:pos + c] = v
        pos += c
    return result


class IsotonicCalibrator:
    """Maps a raw score to a calibrated probability via a monotonic
    step function fit on (raw_score, binary_outcome) pairs -- the
    calibration stage. Monotonic by construction (PAVA), so a higher
    raw score can never map to a LOWER calibrated probability, which a
    naive per-bucket win-rate table doesn't guarantee with sparse
    buckets."""

    def __init__(self):
        self._x_thresholds: np.ndarray | None = None
        self._y_values: np.ndarray | None = None

    def fit(self, raw_scores: np.ndarray, outcomes: np.ndarray) -> "IsotonicCalibrator":
        raw_scores = np.asarray(raw_scores, dtype=float)
        outcomes = np.asarray(outcomes, dtype=float)
        order = np.argsort(raw_scores, kind="stable")
        x_sorted = raw_scores[order]
        y_sorted = outcomes[order]
        self._x_thresholds = x_sorted
        self._y_values = _pava(y_sorted)
        return self

    def calibrate(self, raw_scores: np.ndarray) -> np.ndarray:
        if self._x_thresholds is None or len(self._x_thresholds) == 0:
            return np.full_like(np.asarray(raw_scores, dtype=float), np.nan)
        raw_scores = np.atleast_1d(np.asarray(raw_scores, dtype=float))
        idx = np.searchsorted(self._x_thresholds, raw_scores, side="right") - 1
        idx = np.clip(idx, 0, len(self._y_values) - 1)
        return self._y_values[idx]


@dataclasses.dataclass
class CalibrationResult:
    sample_size: int
    brier_score: float | None
    calibration_valid: bool
    reason: str | None


class CalibratedProbabilityModel:
    """Ties the two stages together and honestly reports whether the
    result should be trusted. `min_sample_size` deliberately defaults
    stricter than ai_trading_engine._calibrated_probability's existing
    CALIBRATION_MIN_SAMPLE=5 -- a number used as a hard entry gate
    (per the dual-probability spec) needs more evidence than one used
    as an informational display figure."""

    MIN_SAMPLE_SIZE_DEFAULT = 30

    def __init__(self, *, min_sample_size: int = MIN_SAMPLE_SIZE_DEFAULT):
        self.min_sample_size = min_sample_size
        self.logistic: LogisticModel | None = None
        self.isotonic: IsotonicCalibrator | None = None
        self.result: CalibrationResult | None = None

    def fit(self, X_train: np.ndarray, y_train: np.ndarray,
            X_calib: np.ndarray, y_calib: np.ndarray) -> "CalibratedProbabilityModel":
        X_train = np.asarray(X_train, dtype=float)
        y_train = np.asarray(y_train, dtype=float)
        X_calib = np.asarray(X_calib, dtype=float)
        y_calib = np.asarray(y_calib, dtype=float)

        n_calib = len(y_calib)
        if len(y_train) == 0 or n_calib == 0:
            self.result = CalibrationResult(sample_size=n_calib, brier_score=None,
                                             calibration_valid=False, reason="NO_DATA")
            return self

        n_features = X_train.shape[1]
        self.logistic = LogisticModel(n_features).fit(X_train, y_train)
        raw_calib = self.logistic.raw_score(X_calib)
        self.isotonic = IsotonicCalibrator().fit(raw_calib, y_calib)

        calibrated = self.isotonic.calibrate(raw_calib)
        brier = float(np.mean((calibrated - y_calib) ** 2))
        valid = n_calib >= self.min_sample_size
        self.result = CalibrationResult(
            sample_size=n_calib, brier_score=brier, calibration_valid=valid,
            reason=None if valid else f"INSUFFICIENT_SAMPLE_SIZE (have {n_calib}, need {self.min_sample_size})",
        )
        return self

    def predict(self, X: np.ndarray) -> np.ndarray | None:
        """Returns calibrated probabilities, or None if calibration was
        never fit or was found invalid -- callers must never call this
        and silently accept garbage; check self.result.calibration_valid
        first (or check for None here)."""
        if self.logistic is None or self.isotonic is None or self.result is None or not self.result.calibration_valid:
            return None
        raw = self.logistic.raw_score(np.asarray(X, dtype=float))
        return self.isotonic.calibrate(raw)
