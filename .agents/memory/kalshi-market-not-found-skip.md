---
name: Kalshi market_not_found is a clean skip, not a failure
description: How the Edge Terminal executor classifies a Kalshi order-endpoint 404 market_not_found.
---

When the Edge Terminal executor submits an order and Kalshi answers
`404 market_not_found`, the executor classifies it as a clean `skipped_gate`
(NOT `failed`).

**Why:** Re-queuing skipped picks for games that have already ended is the
dominant cause — a settled/closed market is gone from the order endpoint. The
operator was furious about red "Kalshi rejected order" failures appearing for
fixtures that simply finished; those are non-tradeable, not bugs. A 404 can also
mean a stale/wrong ticker, but the order is un-placeable in every case, so a
failure label (which implies "retry/fix me") is wrong.

**How to apply:** The skip reason is deliberately NON-asserting ("Kalshi market
not found — settled, closed, or ticker no longer open") rather than claiming
"settled", so a genuine wrong-ticker resolver bug isn't masked behind a false
settled-market label — it stays visible if it shows up on a clearly-upcoming
game. If wrong-ticker masking ever becomes a real problem, split the cases by
probing market status (get_market returns status=finalized for a settled market
but 404s for a nonexistent ticker) instead of widening the failure bucket.
