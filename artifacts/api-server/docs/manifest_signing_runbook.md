# Rotating the model-manifest signing key

`scripts/build_models_manifest.py` signs every published manifest with an
ed25519 keypair (task #73). The private key is held offline; the matching
public key is configured on the API as `AXIOM_MANIFEST_PUBLIC_KEY`.
On every boot (and every `/api/models/reload`) `app/models_loader.py`
downloads `exports/models_manifest.json.sig` from the storage bucket and
verifies it against that public key before the loader will mark itself
ready. A leaked storage write key alone is therefore not enough to
substitute a tampered manifest — the attacker also needs the offline
private key.

This runbook covers rotating that keypair (scheduled rotation or
suspected compromise) without a readiness gap.

## Where the offline key lives

The private key is **never** committed to this repository or stored on
the API server's filesystem. The canonical location is the operator
password manager, in the shared "Axiom Infra" vault, under the item
`manifest_signing_ed25519` (PEM-encoded PKCS8, no passphrase). The item
also holds the matching base64 public key for cross-checking against
`AXIOM_MANIFEST_PUBLIC_KEY` in deployment secrets.

When you need it on a workstation to re-sign the manifest, download it
into a tmpfs / ramdisk path (e.g. `/dev/shm/axiom_signing.pem`), `chmod
600`, run the publish step, and `shred`/`rm` it immediately. Do not
leave the private key on a developer laptop's persistent disk.

## When to rotate

* **Scheduled.** Every 12 months, even if nothing has gone wrong.
  Calendar reminder owned by the on-call operator.
* **Suspected compromise.** Any time the password manager item, the
  laptop that last held the key, or the offline backup may have been
  exposed. Rotate the same day; do not wait for the scheduled window.

## Step 1 — Generate the new keypair (offline workstation)

```bash
cd artifacts/api-server
python scripts/build_models_manifest.py \
    --gen-key /dev/shm/axiom_signing.new.pem
```

The script prints the matching public key as base64 — copy it now,
you'll paste it into `AXIOM_MANIFEST_PUBLIC_KEY` in step 3. The PEM
file is written with mode `0600`. Do **not** delete the OLD key yet:
you still need it in case you have to abort the rotation between
steps 2 and 4.

## Step 2 — Publish a re-signed manifest with the NEW key

Build and upload a fresh manifest signed with the new private key
*before* touching the API's environment. Run from the same workstation
that currently produces production manifests, against the same models
tree that is already deployed (so the published manifest's sha256 set
matches what the loader sees on disk):

```bash
python scripts/build_models_manifest.py \
    --models-dir <path/to/models-tree> \
    --version $(date +%Y.%m.%d)-rotate \
    --out exports/models_manifest.json \
    --sign-key /dev/shm/axiom_signing.new.pem
```

Upload the resulting `models_manifest.json` and `models_manifest.json.sig`
to `<bucket>/exports/`, overwriting the previous pair. The chunked
`models_part_*` archives are unchanged and stay in place.

> **Do not restart the API or hit `POST /api/models/reload` yet.** The
> running instance still has the OLD public key cached in `Settings`
> (`get_settings()` is `@lru_cache`d), so a re-verification at this
> moment would download the new signature, fail to verify it against
> the old pubkey, and refuse to mark ready. The running process keeps
> serving traffic happily because it does *not* re-verify mid-flight —
> verification only runs inside `ensure_artifacts()` on boot or
> on-demand reload.

## Step 3 — Swap `AXIOM_MANIFEST_PUBLIC_KEY` and restart

In the deployment secrets pane (Replit Deployments → Secrets, or the
equivalent for the target environment), replace the value of
`AXIOM_MANIFEST_PUBLIC_KEY` with the base64 public key printed in
step 1. Then restart the `artifacts/api-server: API Server` workflow
(or trigger a redeploy in production) so the new env value is picked
up.

Because the new signature is already published in step 2, the very
first boot with the new pubkey verifies cleanly — no readiness gap.

## Step 4 — Verify the new signature is in effect

Hit the loader status endpoint and confirm both `signature` markers
report against the new key. `/api/models/status` is gated by
`require_auth` (Clerk JWT), so pass an operator session token:

```bash
curl -sS -H "Authorization: Bearer $CLERK_SESSION_TOKEN" \
  "$AXIOM_API_BASE/api/models/status" \
  | python -m json.tool
```

Look for:

```json
{
  "manifest": {
    "version": "2026.04.24-rotate",
    "signature": "ok",
    ...
  },
  "manifest_check": {
    "ok": true,
    "signature": "ok",
    "missing": [],
    "mismatch": [],
    ...
  }
}
```

The matching health card is exposed at `/api/health` under the
`models` probe key (i.e. `/api/health.models` in task language) —
expect `manifest_ok=true` with the card status `ok`. Loader logs at
boot also print `manifest signature OK (ed25519)` from
`_verify_manifest_signature`.

If verification failed, the card surfaces `signature_invalid` as the
reason and the loader stays not-ready. The bundled
`model_integrity_failure` alert pages the operator on the first
failure of the boot cycle (suppressed on retries until
`ensure_artifacts(force=True)` is called).

## Step 5 — Retire the old key

Once `/api/models/status` shows `signature: "ok"` against the new key
in every environment that consumes the manifest:

1. Update the password manager item `manifest_signing_ed25519` —
   replace the PEM and public-key fields with the new values, and
   move the prior values to a dated history entry on the same item.
2. `shred -u /dev/shm/axiom_signing.new.pem` (and any other copies
   you used during the rotation).
3. Note the rotation date in the on-call rotation log so the next
   scheduled rotation is 12 months out.

## Aborting mid-rotation

If something looks wrong between steps 2 and 3 (e.g. you discover the
new manifest has a stale sha set), restore the previous state by
re-running `build_models_manifest.py` against the same models tree
with the **OLD** `--sign-key` and re-uploading. The running API stays
ready throughout because it never re-verified. Once the old signature
is back in storage you can safely restart.

If you have already completed step 3 and need to roll back, repeat
steps 2 + 3 with the OLD keypair: re-sign the manifest with the old
private key, upload, restore the old `AXIOM_MANIFEST_PUBLIC_KEY`
value, and restart.
