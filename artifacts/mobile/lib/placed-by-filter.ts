import { useEffect, useState } from "react";

/**
 * Trade-provenance filter (T005, mobile counterpart of the web
 * `<PlacedByProvider>`).
 *
 * Mirrors the FastAPI ``Literal['manual','system','all']`` query param
 * exposed on /api/dashboard/{overview,recent-trades,vertical-performance,verticals}.
 * We use the same module-level pub/sub pattern as
 * `vertical-filter.ts` so:
 *   - The selected segment survives navigation between tabs (no
 *     React-Context dance, the tabs each mount their own roots).
 *   - Multiple consumers (Dashboard, Positions, Verticals tabs) stay
 *     in lock-step on a single source of truth.
 *
 * `all` is the documented default and means "no server-side filter" —
 * the API treats it as no WHERE clause, surfacing every row including
 * the ``rh_import`` and ``unknown`` provenance buckets.
 */

export type PlacedByFilter = "manual" | "system" | "all";

const DEFAULT_FILTER: PlacedByFilter = "all";

let _placedBy: PlacedByFilter = DEFAULT_FILTER;
const _listeners = new Set<(v: PlacedByFilter) => void>();

function emit(): void {
  for (const l of _listeners) l(_placedBy);
}

export function getPlacedBy(): PlacedByFilter {
  return _placedBy;
}

export function setPlacedBy(v: PlacedByFilter): void {
  if (_placedBy === v) return;
  _placedBy = v;
  emit();
}

/**
 * Subscribe to the shared placed_by selection.  Returns the current
 * value plus a setter; consumers wire the setter into their toggle
 * UI and pass the value into the corresponding API query string.
 */
export function usePlacedBy(): {
  placedBy: PlacedByFilter;
  setPlacedBy: (v: PlacedByFilter) => void;
} {
  const [placedBy, setLocal] = useState<PlacedByFilter>(_placedBy);

  useEffect(() => {
    const sub = (next: PlacedByFilter) => setLocal(next);
    _listeners.add(sub);
    return () => {
      _listeners.delete(sub);
    };
  }, []);

  return { placedBy, setPlacedBy };
}

/**
 * Resets the module-level state.  Test-only helper — production code
 * should never call this since the toggle is meant to persist across
 * tab navigation within a session.
 */
export function __resetPlacedByForTests(): void {
  _placedBy = DEFAULT_FILTER;
  _listeners.clear();
}
