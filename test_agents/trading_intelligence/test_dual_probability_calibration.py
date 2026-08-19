"""Unit tests for agents/trading_intelligence/dual_probability_calibration.py."""
import numpy as np
import pytest

from agents.trading_intelligence.dual_probability_calibration import (
    CalibratedProbabilityModel, IsotonicCalibrator, LogisticModel,
    logit, sigmoid, walk_forward_split,
)


class TestSigmoidLogitNumericalStability:
    def test_sigmoid_extreme_values_do_not_overflow(self):
        assert sigmoid(1e10) == pytest.approx(1.0)
        assert sigmoid(-1e10) == pytest.approx(0.0)
        assert not np.isnan(sigmoid(1e10))
        assert not np.isnan(sigmoid(-1e10))

    def test_sigmoid_zero_is_half(self):
        assert sigmoid(0.0) == pytest.approx(0.5)

    def test_logit_inverts_sigmoid(self):
        x = 2.5
        assert logit(sigmoid(x)) == pytest.approx(x, abs=1e-3)

    def test_logit_does_not_blow_up_at_boundaries(self):
        assert np.isfinite(logit(0.0))
        assert np.isfinite(logit(1.0))


class TestWalkForwardSplit:
    def test_split_is_chronological_never_shuffled(self):
        train, calib, valid = walk_forward_split(100, train_frac=0.6, calib_frac=0.2)
        assert train == slice(0, 60)
        assert calib == slice(60, 80)
        assert valid == slice(80, 100)

    def test_rejects_invalid_fractions(self):
        with pytest.raises(ValueError):
            walk_forward_split(100, train_frac=0.7, calib_frac=0.5)


class TestIsotonicMonotonicity:
    def test_calibrated_output_is_never_decreasing_in_raw_score(self):
        rng = np.random.default_rng(42)
        raw = rng.uniform(-3, 3, size=500)
        # true relationship IS monotonic but noisy
        true_p = sigmoid(raw)
        outcomes = (rng.uniform(size=500) < true_p).astype(float)
        cal = IsotonicCalibrator().fit(raw, outcomes)

        probe = np.linspace(-3, 3, 50)
        out = cal.calibrate(probe)
        assert np.all(np.diff(out) >= -1e-12)  # non-decreasing

    def test_output_is_bounded_0_1(self):
        rng = np.random.default_rng(1)
        raw = rng.uniform(-5, 5, size=200)
        outcomes = rng.integers(0, 2, size=200).astype(float)
        cal = IsotonicCalibrator().fit(raw, outcomes)
        out = cal.calibrate(np.linspace(-10, 10, 20))
        assert np.all(out >= 0.0) and np.all(out <= 1.0)


class TestLogisticFit:
    def test_recovers_a_clean_separable_signal(self):
        rng = np.random.default_rng(7)
        n = 2000
        X = rng.normal(size=(n, 1))
        true_p = sigmoid(3.0 * X[:, 0])
        y = (rng.uniform(size=n) < true_p).astype(float)
        model = LogisticModel(n_features=1).fit(X, y, lr=0.5, epochs=1000)
        # coefficient should recover roughly the true slope's sign and rough magnitude
        assert model.coef_[0] > 1.0

    def test_handles_zero_samples_without_crashing(self):
        model = LogisticModel(n_features=2)
        model.fit(np.empty((0, 2)), np.empty(0))
        assert model.coef_.shape == (2,)


class TestCalibratedProbabilityModel:
    def test_reports_invalid_below_min_sample_size(self):
        rng = np.random.default_rng(3)
        X_train = rng.normal(size=(50, 2))
        y_train = rng.integers(0, 2, size=50).astype(float)
        X_calib = rng.normal(size=(5, 2))  # below default min_sample_size=30
        y_calib = rng.integers(0, 2, size=5).astype(float)

        model = CalibratedProbabilityModel().fit(X_train, y_train, X_calib, y_calib)
        assert model.result.calibration_valid is False
        assert model.predict(X_train) is None

    def test_reports_valid_and_predicts_above_min_sample_size(self):
        rng = np.random.default_rng(11)
        n = 500
        X = rng.normal(size=(n, 2))
        true_p = sigmoid(2.0 * X[:, 0] - 1.0 * X[:, 1])
        y = (rng.uniform(size=n) < true_p).astype(float)
        train, calib, _valid = (slice(0, 300), slice(300, 500), slice(500, 500))

        model = CalibratedProbabilityModel(min_sample_size=30).fit(
            X[train], y[train], X[calib], y[calib]
        )
        assert model.result.calibration_valid is True
        assert model.result.sample_size == 200
        preds = model.predict(X[calib])
        assert preds is not None
        assert np.all((preds >= 0) & (preds <= 1))

    def test_brier_score_is_better_than_random_guessing_on_a_real_signal(self):
        rng = np.random.default_rng(23)
        n = 800
        X = rng.normal(size=(n, 1))
        true_p = sigmoid(4.0 * X[:, 0])
        y = (rng.uniform(size=n) < true_p).astype(float)
        train, calib = slice(0, 500), slice(500, 800)

        model = CalibratedProbabilityModel(min_sample_size=30).fit(X[train], y[train], X[calib], y[calib])
        # a coin-flip (p=0.5 always) has Brier score 0.25 -- a real signal should beat that
        assert model.result.brier_score < 0.25

    def test_no_data_reports_invalid_not_a_crash(self):
        model = CalibratedProbabilityModel().fit(
            np.empty((0, 2)), np.empty(0), np.empty((0, 2)), np.empty(0)
        )
        assert model.result.calibration_valid is False
        assert model.result.reason == "NO_DATA"
