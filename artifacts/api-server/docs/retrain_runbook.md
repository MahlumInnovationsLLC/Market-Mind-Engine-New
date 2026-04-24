# Retraining the meta-model after promoting a UW signal

The autoresearch pipeline mines candidate UW (Unusual Whales) signals
nightly, scores them with IC / Sharpe / monotonicity, and parks them
in `autoresearch_features` with `status = 'shadow'`. Promoting a
candidate flips the row to `status = 'production'`, which:

1. Adds the candidate to the live feature vector (the column shows
   up the next time `app.features.vector.build_vector` runs).
2. Surfaces the candidate in `/api/admin/autoresearch` decision logs.

The live trained meta-model still has its **original column count**
until you publish a fresh `meta_model.pkl` whose `n_features_in_`
matches the expanded layout. Until then the predictor's input
coercion path silently truncates the new column away — i.e. promotion
takes effect for telemetry but not for predictions. Run the offline
retrain pipeline to close the loop.

### How the bridge works

The feature registry only ever returns specs that have been
programmatically registered. Mining writes candidates to
`autoresearch_features` rows, so when a row is promoted we have a
*name* but no matching `FeatureSpec` yet. `app/autoresearch/feature_bridge.py`
fills that gap: `ensure_shadow_spec(name, default=…)` synthesises a
`FeatureSpec(shadow=True)` whose compute reads
`candidate.uw_signals[name]` (with the stored default for missing
snapshots). Both the API startup hook and the admin promote endpoint
call `register_candidates(rows)` immediately before
`set_promoted_names(...)`, so promotion takes effect in the live
vector on the very next prediction. The retrain CLI does the same on
the offline side via `register_names(names)` and aborts with a
non-zero exit if any promoted name fails to register — preventing a
silent publish of an unchanged column count.

The cycle is responsible for populating `candidate["uw_signals"]`
with a snapshot of the same aggregations the mining job uses
offline; until that snapshot is wired in, promoted columns will land
in the vector at their `default` value (0.0 by default), which is
still the correct contract for column alignment. Wiring the snapshot
is tracked as a follow-up.

## 1. Promote a candidate (admin operator)

From the admin console (or `POST /api/admin/autoresearch/{name}`):

```json
{ "decision": "promote" }
```

The API both flips the DB row to `status = 'production'` and refreshes
the in-process promoted set so the live feature vector immediately
includes the new column. (The matching trained model still needs to
be retrained — see below.)

## 2. Run the offline retrain pipeline

From a host with `DATABASE_URL` (or `AXIOM_DATABASE_URL`) pointed at
the production database and the api-server source checked out:

```bash
cd artifacts/api-server
python scripts/retrain_meta_model.py --dry-run
```

`--dry-run` prints the expanded column list — built-in production
features plus every candidate currently in `status = 'production'`,
in the registry's declared order. Use this to confirm the promotion
is visible to the trainer.

When the column list looks right, drop `--dry-run` and run the real
fit:

```bash
python scripts/retrain_meta_model.py \
    --out models/saved/meta_model.pkl \
    --rows 25000 \
    --seed 0
```

This writes two files:

* `models/saved/meta_model.pkl` — the fitted `LogisticRegression`
  whose `n_features_in_` matches the expanded column count.
* `models/saved/meta_model.pkl.feature_names.json` — sidecar JSON
  listing the column order, the promoted set used at training time,
  and the in-sample fit metrics (accuracy, log_loss, n_rows,
  n_features). Future debugging starts here: if a live prediction
  looks off, diff this sidecar against
  `app.features.vector.production_names()` to spot column drift.

## 3. Publish the artifact + reload the API

Build the manifest and ship the artifact through the existing
[`build_models_manifest.py`](../scripts/build_models_manifest.py)
pipeline so the loader picks it up on the next reload:

```bash
python scripts/build_models_manifest.py \
    --models-dir <path/to/models-tree> \
    --version $(date +%Y.%m.%d) \
    --out exports/models_manifest.json \
    --sign-key /secure/axiom_manifest_signing.pem
```

Upload the new manifest + chunked archives to the storage bucket and
either restart the API or hit `POST /api/models/reload` to invalidate
the predictor's cached selection. The next `predict()` call will bind
to the new artifact, see `n_features_in_ == len(columns)`, and accept
the expanded live feature vector unchanged.

## 4. Roll back

If the new model misbehaves in production, you have two safe outs:

* **Demote the candidate** — `POST /api/admin/autoresearch/{name}`
  with `{ "decision": "reset" }`. The promoted set shrinks back, the
  live builder stops emitting the column, and the predictor's
  truncate-to-`n_features_in_` path keeps the old artifact working
  on the shorter vector. (You'll then want to retrain again to get
  back to the canonical column count.)
* **Pin the previous model version** via `POST /api/admin/predictor`
  with the old `models/versions/v…/meta_model.pkl` name. The override
  is persisted in `system_settings` and survives reloads until you
  clear it.

## 5. Notes on training data

The current scaffold trains on a deterministic synthetic matrix
calibrated to `app/features/baselines/training_v1.json`. The matrix
exists to satisfy the column-count contract — it is *not* a real
training distribution. The cold-storage trainer (operated out-of-band
by the modelling team) feeds the same script a real settled-trade
extract via the same `--rows`/`--seed`-style interface; the only
production-relevant pieces of this script are:

1. Reading `autoresearch_features WHERE status = 'production'`.
2. Asking the registry for the expanded column layout in declared
   order.
3. Writing the model + sidecar to the canonical paths.

Anything that produces a sklearn-compatible classifier with the
right `n_features_in_` and column order can be dropped in for the
synthetic matrix block without further changes.

## 6. Manifest signature enforcement

The model loader downloads `models_manifest.json` together with a
detached ed25519 signature (`models_manifest.json.sig`) and verifies
the signature on every cold boot before predictors are loaded. The
verification step is gated by the `AXIOM_MANIFEST_PUBLIC_KEY`
environment variable.

**`AXIOM_MANIFEST_PUBLIC_KEY` set** — the loader treats a missing or
mismatched signature as a hard failure. `manifest_check.signature`
on `/api/health.models` is `"ok"` on success or `"invalid"` on
failure (with `manifest_check.reason = "signature_invalid"` and the
underlying error in `manifest_check.error`). The operator dashboard
shows a green **Verified** badge on the "Model artifacts" card when
the signature checks out, or a red **Invalid** badge with the error
message when it does not.

**`AXIOM_MANIFEST_PUBLIC_KEY` unset** — the loader logs a warning
and continues without verifying the signature. This is the
"legacy" / development mode and `manifest_check.signature` is
reported as `"skipped"`. The dashboard shows a muted **Not
enforced** badge so operators know the deployment is not protected
against manifest tampering.

To enable enforcement on a deployment, set the environment variable
to the base64-encoded ed25519 public key that pairs with the
`--sign-key` private key used by `build_models_manifest.py`:

```bash
# from the same keypair used to sign the manifest:
python -c "from cryptography.hazmat.primitives.serialization import load_pem_private_key; \
import base64, sys; \
sk = load_pem_private_key(open(sys.argv[1],'rb').read(), password=None); \
print(base64.b64encode(sk.public_key().public_bytes_raw()).decode())" \
  /secure/axiom_manifest_signing.pem
```

Set the resulting string as `AXIOM_MANIFEST_PUBLIC_KEY` (Replit
secret in production; `.env` for local dev) and restart the API
workflow. The next boot will surface `signature = "ok"` on the
dashboard.
