"""Replay 2026 SYSTEM trades through the current `GATES` tuple to
quantify what the new tail-risk gates (#743) would have prevented
(task #745).

For every trade in the YTD 2026 audit cohort
(``placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')``,
``executed_at >= 2026-01-01``) the script:

  1. Reconstructs a `RiskContext` from the persisted trade row
     (price, qty, side, vertical, league + the model_edge /
     model_prob / kelly snapshot stored alongside the row).
  2. Runs every gate in the current ``app.risk.gates.GATES`` tuple,
     in declaration order, with **all side effects suppressed**:
       * ``_emit_tail_risk_trip_alert`` is monkey-patched to a no-op
         so an audit replay does not spam operator email/Telegram
         or mutate the alert dedupe table.
       * ``_persist_risk_gate_trip`` is monkey-patched to a no-op so
         the daily honesty report's ``gate_trips_24h`` panel is not
         polluted by replay traffic.
     The script also threads operator overrides that disable the
     globally-stateful gates (``kill_switch``, ``daily_loss``,
     ``vertical_halted``, ``uncalibrated_vertical``) — those gates
     reflect *current* operator settings, not the settings in force
     at trade time, so leaving them on would conflate live ops state
     with the historical counterfactual.
  3. Writes one CSV row per trade with the FIRST veto reason from
     the entire stack PLUS dedicated boolean columns for the two
     new tail-risk gates so an analyst can attribute "% prevented"
     to either gate independently of where it sat in the tuple.
  4. Aggregates the headline counterfactual: trades vetoed by EITHER
     new gate, $-loss prevented (sum of negative PnLs on those
     trades), $-profit forfeited (sum of positive PnLs on those
     trades), and the implied net delta vs. the actual books.

Modes:

* ``--from-db``        — pull SYSTEM trades from the live DB.
* ``--from-json PATH`` — load trades from a JSON list (offline /
  test fixtures so the pytest harness can assert exact dollars
  without a DB).

Per-vertical override knobs can be threaded in via ``--override
key=value`` (repeatable) so the looser crypto cap suggested by the
audit is reflected in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app.risk import gates as _gates  # noqa: E402


# Audit cohort filter — placed_by tags written by the autonomous trade
# placement paths.  Matches the YTD audit query used in task #743.
SYSTEM_PLACED_BY = ("orchestrator", "autotrader", "swarm", "operator_recovery")

# The two gates this replay attributes the headline "savings" to.
TAIL_RISK_GATE_NAMES = frozenset({"tail_price_size_cap", "jumbo_quantity_cap"})

# Gates whose verdict reflects *current* operator state, not the state
# at trade time, and so cannot be honestly replayed.  We disable them
# via operator overrides so the replay attributes vetoes only to the
# data-pure gates we actually want to study.
_REPLAY_OVERRIDES_BASE: dict[str, Any] = {
    # kill_switch: force paper so a current "halt" does not blanket-veto.
    "trading_mode": "paper",
    # daily_loss: leaving thresholds None → gate is a documented no-op.
    # vertical_halted: omitting `vertical_halted.<v>` keys → no-op.
}

# Per-vertical raw-mode overrides for `gate_uncalibrated_vertical` —
# the gate refuses to ship a trade whose (vertical, league) lacks a
# fitted calibrator snapshot, but at trade time those calibrators DID
# exist (the trade actually shipped), so during replay we opt every
# vertical into raw mode so the gate passes.
_RAW_MODE_VERTICALS = (
    "sports", "crypto", "weather", "events_econ", "tech",
    "election", "entertainment", "intangibles", "commodities",
)


@dataclass
class TradeRow:
    """Minimal projection of `trades` (+ league join) the gates need."""
    trade_id: str
    market_id: str
    side: str               # 'YES' | 'NO'
    vertical: str
    league: str | None
    quantity: int
    price: float            # YES-side mid at fill (0..1)
    pnl: float | None       # realised P&L; None == still open
    # Persisted at trade time — used to reconstruct ctx.edge,
    # ctx.model_probability, ctx.kelly so the gates that read those
    # fields don't degrade or mis-veto a row that originally shipped.
    model_prob: float | None = None
    model_edge: float | None = None
    kelly_fraction: float | None = None

    @property
    def notional_usd(self) -> float:
        return float(self.quantity) * float(self.price)


def _normalise_side(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in ("yes", "buy_yes", "buy yes", "live_yes"):
        return "YES"
    if s in ("no", "buy_no", "buy no", "live_no"):
        return "NO"
    return "YES" if not s else s.upper()


def _build_overrides(user_overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge user overrides on top of the replay-safe defaults."""
    out: dict[str, Any] = dict(_REPLAY_OVERRIDES_BASE)
    for v in _RAW_MODE_VERTICALS:
        out[f"uncalibrated_raw_mode_{v}"] = True
    out.update(user_overrides or {})
    return out


def _ctx_for_trade(t: TradeRow, overrides: dict[str, Any]) -> _gates.RiskContext:
    """Build a `RiskContext` faithful enough for the GATES tuple to
    evaluate without spuriously vetoing a trade that originally
    shipped.  Bands / agreement default to None so the corresponding
    gates short-circuit to PASS exactly as they do at production
    when the swarm did not report them."""
    return _gates.RiskContext(
        market_id=t.market_id,
        side=t.side,
        vertical=(t.vertical or "").lower(),
        league=t.league,
        market_type=None,
        matchup=None,
        current_price=t.price,
        model_probability=t.model_prob,
        # Use the persisted edge; fall back to a comfortably positive
        # value so `gate_min_edge` doesn't blanket-veto rows whose
        # model_edge column is NULL (older bot versions).  A trade
        # that actually shipped MUST have cleared min_edge at trade
        # time; we reflect that by ensuring the gate passes here too.
        edge=t.model_edge if t.model_edge is not None else 1.0,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES" if t.side == "YES" else "BUY_NO",
        debate_id=None,
        intended_quantity=int(t.quantity),
        intended_notional_usd=t.notional_usd,
        bankroll_usd=None,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides,
        market_cooldown_until=None,
    )


@contextmanager
def _suppress_gate_side_effects():
    """Temporarily neuter alert dispatch + risk_gate_trips persistence.

    Reading historical trades to evaluate "what would the gates have
    done?" must NEVER fan out an email, write to the alert dedupe
    table, or pollute the daily honesty report's gate-trips panel.
    Both helpers are async; we replace them with no-op coroutines for
    the duration of the replay only.
    """
    saved_alert = _gates._emit_tail_risk_trip_alert
    saved_persist = _gates._persist_risk_gate_trip

    async def _noop(*_args, **_kwargs):  # pragma: no cover — trivial
        return None

    _gates._emit_tail_risk_trip_alert = _noop  # type: ignore[assignment]
    _gates._persist_risk_gate_trip = _noop  # type: ignore[assignment]
    try:
        yield
    finally:
        _gates._emit_tail_risk_trip_alert = saved_alert  # type: ignore[assignment]
        _gates._persist_risk_gate_trip = saved_persist  # type: ignore[assignment]


async def _evaluate_one(
    t: TradeRow,
    overrides: dict[str, Any],
    *,
    tail_only: bool,
) -> dict[str, Any]:
    """Run gates against a single trade and return one report row.

    Default mode iterates the full ``GATES`` tuple in order so the
    report reflects the production gate stack.  ``tail_only`` skips
    everything except the two new tail-risk gates — useful when the
    caller wants to attribute counterfactual savings without any
    confound from the rest of the stack.
    """
    ctx = _ctx_for_trade(t, overrides)
    if tail_only:
        gates_to_run = (
            ("tail_price_size_cap", _gates.gate_tail_price_size_cap),
            ("jumbo_quantity_cap", _gates.gate_jumbo_quantity_cap),
        )
    else:
        gates_to_run = _gates.GATES

    first_veto: tuple[str, str] | None = None
    tail_passed = True
    jumbo_passed = True
    all_results: dict[str, bool] = {}
    for name, fn in gates_to_run:
        try:
            res = await fn(ctx)
        except Exception as e:  # pragma: no cover — defensive
            res = _gates.GateResult(
                name, True, f"replay degraded: {e}", {"error": str(e)},
            )
        all_results[name] = bool(res.passed)
        if name == "tail_price_size_cap":
            tail_passed = bool(res.passed)
        elif name == "jumbo_quantity_cap":
            jumbo_passed = bool(res.passed)
        if not res.passed and first_veto is None:
            first_veto = (name, res.reason or "")

    tail_risk_vetoed = (not tail_passed) or (not jumbo_passed)
    veto_attrib = (
        "tail_price_size_cap" if not tail_passed
        else "jumbo_quantity_cap" if not jumbo_passed
        else ""
    )
    return {
        "trade_id": t.trade_id,
        "market_id": t.market_id,
        "vertical": t.vertical,
        "league": t.league or "",
        "side": t.side,
        "quantity": t.quantity,
        "price": t.price,
        "notional_usd": round(t.notional_usd, 2),
        "pnl": "" if t.pnl is None else round(float(t.pnl), 2),
        "first_veto_gate": first_veto[0] if first_veto else "",
        "first_veto_reason": first_veto[1] if first_veto else "",
        "tail_passed": tail_passed,
        "jumbo_passed": jumbo_passed,
        "tail_risk_vetoed": tail_risk_vetoed,
        "tail_risk_attrib": veto_attrib,
    }


async def replay(
    trades: Iterable[TradeRow],
    overrides: dict[str, Any] | None = None,
    *,
    tail_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the replay.  Pure (no DB) so the pytest harness can call
    it with synthetic trades."""
    user_overrides = overrides or {}
    effective_overrides = _build_overrides(user_overrides)
    rows: list[dict[str, Any]] = []
    with _suppress_gate_side_effects():
        for t in trades:
            rows.append(await _evaluate_one(
                t, effective_overrides, tail_only=tail_only,
            ))
    summary = _summarise(rows)
    summary["mode"] = "tail_only" if tail_only else "full_gates"
    summary["overrides_applied"] = dict(user_overrides)
    return rows, summary


def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the headline counterfactual numbers.

    "Vetoed" here means *vetoed by either of the two new tail-risk
    gates*.  Other gates' vetoes are reported in `first_veto_gate`
    on the per-trade rows but are NOT counted as "tail-risk savings"
    so the headline number isolates the contribution of task #743.
    """
    settled = [r for r in rows if r["pnl"] != ""]
    n_total = len(rows)
    n_settled = len(settled)
    n_vetoed = sum(1 for r in rows if r["tail_risk_vetoed"])

    prevented_loss = sum(
        -float(r["pnl"]) for r in settled
        if r["tail_risk_vetoed"] and float(r["pnl"]) < 0
    )
    forfeited_profit = sum(
        float(r["pnl"]) for r in settled
        if r["tail_risk_vetoed"] and float(r["pnl"]) > 0
    )
    actual_net_pnl = sum(float(r["pnl"]) for r in settled)
    counterfactual_net_pnl = actual_net_pnl + prevented_loss - forfeited_profit
    total_audit_loss = sum(
        -float(r["pnl"]) for r in settled if float(r["pnl"]) < 0
    )
    pct_audit_losses_prevented = (
        100.0 * prevented_loss / total_audit_loss
        if total_audit_loss > 0 else 0.0
    )

    by_gate: dict[str, dict[str, Any]] = {}
    for gate in ("tail_price_size_cap", "jumbo_quantity_cap"):
        gate_rows = [r for r in rows if r["tail_risk_attrib"] == gate]
        gate_settled = [r for r in gate_rows if r["pnl"] != ""]
        by_gate[gate] = {
            "vetoed": len(gate_rows),
            "settled_vetoed": len(gate_settled),
            "loss_prevented_usd": round(sum(
                -float(r["pnl"]) for r in gate_settled
                if float(r["pnl"]) < 0
            ), 2),
            "profit_forfeited_usd": round(sum(
                float(r["pnl"]) for r in gate_settled
                if float(r["pnl"]) > 0
            ), 2),
        }

    return {
        "trades_total": n_total,
        "trades_settled": n_settled,
        "trades_vetoed": n_vetoed,
        "veto_rate_pct": round(100.0 * n_vetoed / n_total, 2) if n_total else 0.0,
        "actual_net_pnl_usd": round(actual_net_pnl, 2),
        "loss_prevented_usd": round(prevented_loss, 2),
        "profit_forfeited_usd": round(forfeited_profit, 2),
        "counterfactual_net_pnl_usd": round(counterfactual_net_pnl, 2),
        "counterfactual_delta_usd": round(
            counterfactual_net_pnl - actual_net_pnl, 2
        ),
        "audit_total_loss_usd": round(total_audit_loss, 2),
        "pct_audit_losses_prevented": round(pct_audit_losses_prevented, 2),
        "by_gate": by_gate,
    }


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


async def _load_from_db(start_date: str) -> list[TradeRow]:
    """Pull SYSTEM trades from the live DB.  Imports `app.db` lazily so
    the offline JSON path works without DB env vars set."""
    from datetime import datetime, timezone
    from app import db as _db
    if isinstance(start_date, str):
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    else:
        start_dt = start_date
    rows = await _db.fetch(
        """
        SELECT
            t.trade_id::text                                AS trade_id,
            t.market_id::text                               AS market_id,
            t.side::text                                    AS side,
            coalesce(t.vertical, '')::text                  AS vertical,
            upper(coalesce(t.league, ''))::text             AS league,
            coalesce(t.quantity, 0)::int                    AS quantity,
            coalesce(t.price, 0)::float                     AS price,
            t.pnl::float                                    AS pnl,
            t.model_prob::float                             AS model_prob,
            t.model_edge::float                             AS model_edge,
            t.kelly_fraction::float                         AS kelly_fraction
          FROM trades t
         WHERE t.placed_by = ANY($1::text[])
           AND t.executed_at >= $2::timestamptz
         ORDER BY t.executed_at ASC
        """,
        list(SYSTEM_PLACED_BY),
        start_dt,
    )
    out: list[TradeRow] = []
    for r in rows:
        out.append(TradeRow(
            trade_id=r["trade_id"] or "",
            market_id=r["market_id"] or "",
            side=_normalise_side(r["side"]),
            vertical=(r["vertical"] or "").lower(),
            league=(r["league"] or None),
            quantity=int(r["quantity"] or 0),
            price=float(r["price"] or 0.0),
            pnl=None if r["pnl"] is None else float(r["pnl"]),
            model_prob=None if r["model_prob"] is None else float(r["model_prob"]),
            model_edge=None if r["model_edge"] is None else float(r["model_edge"]),
            kelly_fraction=(
                None if r["kelly_fraction"] is None
                else float(r["kelly_fraction"])
            ),
        ))
    return out


def _load_from_json(path: Path) -> list[TradeRow]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise SystemExit(f"{path}: top-level JSON must be a list of trades")
    out: list[TradeRow] = []
    for i, t in enumerate(raw):
        try:
            out.append(TradeRow(
                trade_id=str(t.get("trade_id") or f"row-{i}"),
                market_id=str(t.get("market_id") or ""),
                side=_normalise_side(t.get("side")),
                vertical=str(t.get("vertical") or "").lower(),
                league=(str(t["league"]).upper() if t.get("league") else None),
                quantity=int(t.get("quantity") or 0),
                price=float(t.get("price") or 0.0),
                pnl=None if t.get("pnl") is None else float(t["pnl"]),
                model_prob=t.get("model_prob"),
                model_edge=t.get("model_edge"),
                kelly_fraction=t.get("kelly_fraction"),
            ))
        except (TypeError, ValueError) as e:
            raise SystemExit(f"{path}: row {i}: {e}") from e
    return out


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _parse_overrides(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw in items or []:
        if "=" not in raw:
            raise SystemExit(f"--override expects key=value, got {raw!r}")
        k, v = raw.split("=", 1)
        try:
            out[k] = float(v) if "." in v else int(v)
        except ValueError:
            out[k] = v
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-db", action="store_true",
                     help="Pull SYSTEM trades from the live trades table.")
    src.add_argument("--from-json", type=Path,
                     help="Load trades from a JSON list (offline / test).")
    p.add_argument("--start-date", default="2026-01-01",
                   help="DB cohort start (YYYY-MM-DD); default 2026-01-01.")
    p.add_argument("--out-csv", type=Path, default=None,
                   help="Per-trade veto report.")
    p.add_argument("--out-json", type=Path, default=None,
                   help="Aggregate summary report.")
    p.add_argument("--tail-only", action="store_true",
                   help="Run only the two new tail-risk gates (skip the "
                        "rest of GATES).  Default = run the full tuple.")
    p.add_argument("--override", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="Operator override (repeatable). e.g. "
                        "tail_price_threshold.crypto=0.05")
    return p


async def _main_async(args: argparse.Namespace) -> int:
    if args.from_db:
        trades = await _load_from_db(args.start_date)
    else:
        trades = _load_from_json(args.from_json)
    overrides = _parse_overrides(args.override)
    rows, summary = await replay(
        trades, overrides=overrides, tail_only=args.tail_only,
    )
    if args.out_csv:
        _write_csv(rows, args.out_csv)
    if args.out_json:
        args.out_json.write_text(json.dumps(summary, indent=2))
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
