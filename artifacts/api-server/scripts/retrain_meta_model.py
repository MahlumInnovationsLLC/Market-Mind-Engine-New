"""Offline retrain pipeline for the binary meta-model.

Reads ``autoresearch_features WHERE status = 'production'`` from the
database to discover which UW candidates an operator has promoted,
expands the registry's built-in column list with those promoted shadow
specs (in the registry's declared order), trains a fresh sklearn
LogisticRegression on a synthesised matrix calibrated to the shipped
training-time baselines, and writes the resulting model to
``models/saved/meta_model.pkl`` along with a sidecar
``meta_model.feature_names.json`` describing the expanded column layout.

The sidecar is what closes the "promote → predict" loop: the live
feature builder (``app.features.vector.build_vector``) emits the
expanded vector at runtime once the API hydrates its promoted set
from the same DB rows, and the trained ``meta_model.pkl`` accepts that
wider vector because its ``n_features_in_`` was set to match.

Older meta-model artifacts (from before the promoted set existed) keep
working: the predictor's ``_coerce_features`` already pads / truncates
the supplied vector to whichever ``n_features_in_`` the loaded model
advertises, so a 32-column model deployed against a 33-column live
builder simply drops the last column rather than crashing.

Usage
-----

    AXIOM_DATABASE_URL=postgresql://... \\
        python scripts/retrain_meta_model.py \\
            --out models/saved/meta_model.pkl \\
            --rows 25000 \\
            --seed 0

Pass ``--dry-run`` to print the expanded column list without writing a
model artifact (handy for verifying which UW candidates would be picked
up before kicking off a real fit).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

# Make `app.*` importable when the script is run from the api-server dir.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.autoresearch import feature_bridge as ar_feature_bridge  # noqa: E402
from app.autoresearch import store as ar_store  # noqa: E402
from app.features.registry import registry as feature_registry  # noqa: E402


log = logging.getLogger("axiom.retrain")


def _load_baselines() -> dict[str, dict[str, float]]:
    """Return ``{feature_name: {mean, std}}`` from the shipped JSON."""
    path = (
        _ROOT / "app" / "features" / "baselines" / "training_v1.json"
    )
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("baselines file not found at %s; using N(0.5, 0.2)", path)
        return {}
    return {
        name: {
            "mean": float(row.get("mean", 0.5)),
            "std": float(row.get("std", 0.2)) or 0.05,
        }
        for name, row in (raw.get("features") or {}).items()
    }


def _resolve_columns(promoted: Sequence[str]) -> tuple[str, ...]:
    """Built-in production names + promoted shadow names, registry order."""
    return feature_registry.production_names(promoted)


def _synth_training_matrix(
    columns: Sequence[str],
    baselines: dict[str, dict[str, float]],
    n_rows: int,
    seed: int,
) -> tuple["np.ndarray", "np.ndarray"]:
    """Generate a deterministic training matrix calibrated to baselines.

    The model contract here is that the offline pipeline produces a
    classifier with the right ``n_features_in_`` and a non-degenerate
    decision surface; the *real* training distribution lives in the
    cold-storage trainer (which is fed by the operator team's offline
    settled-trade extracts). Promoted UW candidates without a baseline
    entry get N(0, 1) noise so they still contribute a non-zero
    coefficient when the column actually drives the synthesised label.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    n_cols = len(columns)
    X = np.empty((n_rows, n_cols), dtype=float)
    for j, name in enumerate(columns):
        b = baselines.get(name)
        if b is None:
            # Promoted UW candidate — no training-time baseline yet.
            # Centred N(0, 1) is the safest assumption; the trainer
            # will learn a coefficient if the column is informative.
            X[:, j] = rng.normal(0.0, 1.0, size=n_rows)
        else:
            std = max(b["std"], 0.01)
            X[:, j] = rng.normal(b["mean"], std, size=n_rows)
    # Synthesised label: lift on pred_prob if present, plus a small
    # weight on every promoted column so the trainer can't just zero
    # them out. Non-degenerate so LogReg doesn't warn on convergence.
    base = np.zeros(n_rows, dtype=float)
    if "pred_prob" in columns:
        base += 4.0 * (X[:, columns.index("pred_prob")] - 0.5)
    if "edge" in columns:
        base += 6.0 * X[:, columns.index("edge")]
    promoted_cols = [c for c in columns if c.startswith("uw__")]
    for c in promoted_cols:
        base += 0.4 * X[:, columns.index(c)]
    base += rng.normal(0.0, 0.5, size=n_rows)
    p = 1.0 / (1.0 + np.exp(-base))
    y = (rng.uniform(0.0, 1.0, size=n_rows) < p).astype(int)
    return X, y


def train(
    columns: Sequence[str], n_rows: int, seed: int
) -> tuple[object, dict[str, float]]:
    """Fit a LogisticRegression on the synthesised matrix.

    Returns ``(estimator, metrics)`` where ``metrics`` carries the
    in-sample accuracy + log-loss so the caller can sanity-check the
    fit before publishing the artifact.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, log_loss

    baselines = _load_baselines()
    X, y = _synth_training_matrix(columns, baselines, n_rows, seed)
    clf = LogisticRegression(max_iter=500)
    clf.fit(X, y)
    p = clf.predict_proba(X)[:, 1]
    metrics = {
        "n_rows": int(n_rows),
        "n_features": int(len(columns)),
        "accuracy": float(accuracy_score(y, (p >= 0.5).astype(int))),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "positive_rate": float(y.mean()),
    }
    return clf, metrics


def _write_artifact(
    clf: object,
    columns: Sequence[str],
    metrics: dict[str, float],
    out_path: Path,
    promoted: Sequence[str],
) -> dict[str, str]:
    """Serialize the fitted estimator + sidecar JSON next to it."""
    import joblib

    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, out_path)
    sidecar = out_path.with_suffix(out_path.suffix + ".feature_names.json")
    sidecar.write_text(
        json.dumps(
            {
                "model_path": str(out_path.name),
                "columns": list(columns),
                "promoted": list(promoted),
                "metrics": metrics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return {"model": str(out_path), "sidecar": str(sidecar)}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("models/saved/meta_model.pkl"),
        help="Output path for the trained meta-model artifact.",
    )
    ap.add_argument(
        "--rows",
        type=int,
        default=25_000,
        help="Number of synthetic training rows to fit on.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=0,
        help="RNG seed for the synthetic training matrix.",
    )
    ap.add_argument(
        "--dsn",
        default=None,
        help=(
            "Override the Postgres DSN. Resolution order: --dsn, "
            "$AXIOM_DATABASE_URL, settings.supabase_db_dsn (built from "
            "$SUPABASE_DB_PASSWORD + $SUPABASE_PROJECT_REF), $DATABASE_URL."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the expanded column list and exit without training.",
    )
    args = ap.parse_args(argv)

    promoted = ar_store.list_promoted_names_sync(args.dsn)
    # Synthesize matching shadow specs for any promoted name that the
    # built-in builders haven't already registered. Without this the
    # registry's ``production_names`` filter would drop the promoted
    # name on the floor and we'd silently retrain on the original
    # column set — defeating the entire pipeline.
    if promoted:
        registered = ar_feature_bridge.register_names(promoted)
        unresolved = sorted(set(promoted) - set(registered))
        if unresolved:
            # Fail fast — publishing an unchanged artifact while
            # autoresearch_features says a candidate is in production
            # would mask the bug indefinitely.
            log.error(
                "promoted candidates failed to register as shadow specs: %s",
                unresolved,
            )
            return 2
    columns = _resolve_columns(promoted)
    log.info(
        "promoted=%d columns=%d (built-in=%d)",
        len(promoted), len(columns),
        len(feature_registry.names()),
    )
    log.info("expanded column layout: %s", list(columns))

    if args.dry_run:
        print(json.dumps({
            "promoted": promoted,
            "columns": list(columns),
            "n_columns": len(columns),
        }, indent=2))
        return 0

    clf, metrics = train(columns, args.rows, args.seed)
    written = _write_artifact(clf, columns, metrics, args.out, promoted)
    log.info("wrote %s", written["model"])
    log.info("wrote %s", written["sidecar"])
    log.info("metrics: %s", metrics)
    print(json.dumps({"written": written, "metrics": metrics}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
