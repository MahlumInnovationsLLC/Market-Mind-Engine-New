---
name: Edge Terminal resolver stub surface
description: Which resolver method executor calls, so unit-test stubs don't silently skip
---

The Edge Terminal executor resolves a signal through the **structured**
`market_resolver.resolve_match(sig)` surface (returns a `ResolveResult` with
`.ticker`, `.side`, `.reason`, `.kind`), NOT the legacy bare `resolve()`.

**Why:** A unit test that stubs only `resolve()` leaves the real `resolve_match`
running. With a scraper-supplied `kalshi_ticker`, `resolve_match` returns early
(direct ticker is authoritative), so the test passes. But with
`kalshi_ticker=None` the real resolver does DB/series lookups, finds nothing,
and the signal is `skipped_unresolved` — the executor never reaches
`place_order`, so the test sees 0 fires. This is a stale-stub class that hides
as "0 != 1" assertion failures unrelated to the actual change under test.

**How to apply:** When stubbing the resolver in executor/poller unit tests,
stub `resolve_match` to return `ResolveResult(ticker, reason, [], kind="matched")`
(set `side` only to force a resolver-dictated side). Keep stubbing both
`resolve` and `resolve_match` so the harness survives future interface churn.
