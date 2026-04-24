// Shared venue helpers used by the pipeline candidate block, the
// console tail, and the trade-train ticker. Keeping the extraction
// logic in one place ensures a fan-out placement is parsed the same
// way no matter where it surfaces in the operator dashboard.
//
// The orchestrator's `candidate_complete` event can carry the routed
// venue(s) under three shapes — the legacy single `placement.exchange`,
// a `placements: [{exchange, ...}]` list once fan-out is in play, and
// a flat `venues: [...]` convenience array. We accept any of them so
// we work whether the candidate landed on Kalshi, Polymarket, or both.

export const KNOWN_VENUES = ["kalshi", "polymarket"] as const;
export type KnownVenue = (typeof KNOWN_VENUES)[number];

export function normalizeVenue(
  raw: string | null | undefined,
): KnownVenue | "other" | null {
  if (!raw) return null;
  const v = raw.trim().toLowerCase();
  if (!v) return null;
  if ((KNOWN_VENUES as readonly string[]).includes(v)) return v as KnownVenue;
  return "other";
}

export function venueDisplayLabel(raw: string | null | undefined): string {
  const v = normalizeVenue(raw);
  if (v === "kalshi") return "Kalshi";
  if (v === "polymarket") return "Polymarket";
  if (v === "other") return (raw ?? "").toUpperCase();
  return "—";
}

// Lower-cased, deduped, primary-first list of venues a single
// candidate_complete event was routed to. Returns an empty list when
// the event isn't a placed terminal frame, so callers can simply check
// `venues.length > 0` to decide whether to render the badge.
export function extractVenuesFromEvent(
  ev: Record<string, unknown> | null | undefined,
): string[] {
  if (!ev || typeof ev !== "object") return [];
  const out: string[] = [];
  const seen = new Set<string>();
  const add = (raw: unknown) => {
    if (typeof raw !== "string") return;
    const v = raw.trim().toLowerCase();
    if (!v || seen.has(v)) return;
    seen.add(v);
    out.push(v);
  };
  const venuesField = ev["venues"];
  if (Array.isArray(venuesField)) for (const v of venuesField) add(v);
  const placementsField = ev["placements"];
  if (Array.isArray(placementsField)) {
    for (const p of placementsField) {
      if (p && typeof p === "object") {
        add((p as Record<string, unknown>).exchange);
      }
    }
  }
  const placement = ev["placement"];
  if (placement && typeof placement === "object") {
    add((placement as Record<string, unknown>).exchange);
    const nested = (placement as Record<string, unknown>).venues;
    if (Array.isArray(nested)) for (const v of nested) add(v);
  }
  return out;
}
