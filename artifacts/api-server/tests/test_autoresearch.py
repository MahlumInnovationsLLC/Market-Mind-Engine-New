"""Tests for the AutoResearch mining pipeline + shadow feature wiring."""
from __future__ import annotations

import math

import pytest

from app.autoresearch import stats, store
from app.features.registry import FeatureRegistry, FeatureSpec


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def test_pearson_perfect_positive_correlation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert stats.pearson(xs, ys) == pytest.approx(1.0)


def test_pearson_perfect_negative_correlation():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 8.0, 6.0, 4.0, 2.0]
    assert stats.pearson(xs, ys) == pytest.approx(-1.0)


def test_pearson_zero_variance_returns_zero():
    # Constant series have undefined Pearson — must return 0, not NaN.
    assert stats.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert stats.pearson([], []) == 0.0


def test_spearman_handles_ties():
    # Strictly monotone ranks despite tied values in xs.
    xs = [1.0, 1.0, 2.0, 3.0, 4.0]
    ys = [10.0, 11.0, 20.0, 30.0, 40.0]
    s = stats.spearman(xs, ys)
    assert 0.9 < s <= 1.0


def test_sharpe_signal_to_noise():
    # value*residual = [1, 1, 1, 1, 1] — std=0, so Sharpe is 0 by convention.
    assert stats.sharpe([1, 1, 1, 1, 1], [1, 1, 1, 1, 1]) == 0.0
    # Mean strongly positive vs small noise → Sharpe > 0.
    s = stats.sharpe([1, 2, 3, 4, 5], [1, 1, 1, 1, 1])
    assert s > 0


def test_monotonicity_increasing_buckets():
    # Values 0..29 paired with residuals = values/10 → strict monotone.
    n = 30
    values = [float(i) for i in range(n)]
    residuals = [v / 10.0 for v in values]
    assert stats.monotonicity(values, residuals, bins=5) == pytest.approx(1.0)


def test_monotonicity_returns_zero_for_too_few_samples():
    assert stats.monotonicity([1.0, 2.0], [1.0, 2.0], bins=5) == 0.0


def test_evaluate_summary_keys():
    summary = stats.evaluate([1.0, 2.0, 3.0, 4.0], [0.1, 0.2, 0.3, 0.4])
    assert set(summary.keys()) == {"n", "ic", "sharpe", "monotonicity"}
    assert summary["n"] == 4
    assert summary["ic"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Feature registry shadow flag
# ---------------------------------------------------------------------------


def test_registry_shadow_specs_excluded_from_production_view():
    r = FeatureRegistry()
    r.register(FeatureSpec("prod_a", "src", "t", "raw", 0.0, lambda _: 1.0))
    r.register(FeatureSpec(
        "shadow_a", "src", "t", "raw", 0.0, lambda _: 9.0, shadow=True,
    ))
    r.register(FeatureSpec("prod_b", "src", "t", "raw", 0.0, lambda _: 2.0))

    # Production-facing API ignores shadow specs entirely.
    assert r.names() == ("prod_a", "prod_b")
    assert len(r) == 2
    assert r.metadata() == [
        {"index": 0, "name": "prod_a", "source": "src", "transform": "t",
         "dtype": "raw", "default": 0.0, "shadow": False},
        {"index": 1, "name": "prod_b", "source": "src", "transform": "t",
         "dtype": "raw", "default": 0.0, "shadow": False},
    ]
    # Shadow-aware enumeration is opt-in.
    assert {s.name for s in r.shadow_specs()} == {"shadow_a"}
    assert {s.name for s in r.all_specs()} == {"prod_a", "shadow_a", "prod_b"}


def test_registry_build_skips_shadow_from_vector_but_computes_them():
    """build() must compute shadow specs (so derived features and
    AutoResearch telemetry see them) while keeping them out of the
    positional vector that aligns to the trained model."""
    from datetime import datetime, timezone

    r = FeatureRegistry()
    r.register(FeatureSpec("prod_a", "src", "t", "raw", 0.0, lambda _: 1.0))
    captured: dict[str, float] = {}

    def _shadow_compute(bc):
        captured["seen"] = 1.0
        return 7.0

    r.register(FeatureSpec(
        "shadow_x", "src", "t", "raw", 0.0, _shadow_compute, shadow=True,
    ))
    r.register(FeatureSpec("prod_b", "src", "t", "raw", 0.0, lambda _: 3.0))

    values, _present = r.build(
        candidate={}, ctx={}, now=datetime(2025, 1, 1, tzinfo=timezone.utc),
        overrides={},
    )
    # Vector contains only production specs in declared order.
    assert values == [1.0, 3.0]
    # Shadow compute still ran (so we collect telemetry / training rows).
    assert captured == {"seen": 1.0}


def test_registry_metadata_can_include_shadow_for_admin_view():
    r = FeatureRegistry()
    r.register(FeatureSpec("prod", "src", "t", "raw", 0.0, lambda _: 0.0))
    r.register(FeatureSpec(
        "shadow", "src", "t", "raw", 0.0, lambda _: 0.0, shadow=True,
    ))
    md = r.metadata(include_shadow=True)
    assert [row["name"] for row in md] == ["prod", "shadow"]
    assert {row["name"]: row["shadow"] for row in md} == {
        "prod": False, "shadow": True,
    }


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def test_feature_name_is_stable_and_lowercase():
    assert store.feature_name("Sports", "FLOW_ALERT", "Count_24h") == (
        "uw__flow_alert__count_24h__sports"
    )


def test_feature_name_components_separated():
    n = store.feature_name("crypto", "darkpool_print", "sum_premium_24h")
    assert n.startswith("uw__")
    assert "darkpool_print" in n
    assert "sum_premium_24h" in n
    assert n.endswith("crypto")


# ---------------------------------------------------------------------------
# Sanity: live registry production count is unchanged by adding the shadow
# capability (no rows registered as shadow yet — keeps trained model aligned).
# ---------------------------------------------------------------------------


def test_live_registry_unchanged_count():
    from app.features import FEATURE_COUNT, registry as live_registry

    assert len(live_registry) == FEATURE_COUNT == 32
    # And no production spec is accidentally flagged as shadow.
    assert all(not s.shadow for s in live_registry.specs())
