import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useSearch } from "wouter";

/**
 * Trade-provenance filter (T004).
 *
 * Mirrors the FastAPI ``Literal['manual','system','all']`` query param
 * on `/api/dashboard/overview`, `/recent-trades`,
 * `/vertical-performance`, and `/verticals`.  Single source of truth
 * for the dashboard's 3-segment Manual / System / Both toggle so every
 * P&L surface (KPI strip, recent-trades feed, per-vertical breakdown,
 * vertical cards) stays in lock-step.
 *
 * Persistence model:
 *   - Source of truth lives in component state (so re-renders are
 *     synchronous and React-Query hooks see the new value on the same
 *     tick the toggle is clicked).
 *   - Mirrored to the URL ``?placed_by=manual|system|all`` query
 *     string so the toggle state survives reloads and is shareable.
 *   - On mount we hydrate from the URL; on change we replace history
 *     (no push) so the back button doesn't fill with toggle clicks.
 *
 * `all` is the documented default; we omit the param from the URL when
 * the value equals the default to keep shared links clean.
 */

export type PlacedByFilter = "manual" | "system" | "all";

const DEFAULT_FILTER: PlacedByFilter = "all";
const URL_PARAM = "placed_by";

function parsePlacedBy(raw: string | null | undefined): PlacedByFilter {
  if (raw === "manual" || raw === "system" || raw === "all") return raw;
  return DEFAULT_FILTER;
}

type Ctx = {
  placedBy: PlacedByFilter;
  setPlacedBy: (v: PlacedByFilter) => void;
};

const PlacedByContext = createContext<Ctx | null>(null);

export function PlacedByProvider({ children }: { children: ReactNode }) {
  // Wouter's useSearch returns the raw query string (e.g. "?foo=bar"
  // or just "foo=bar" depending on version) — feed it through
  // URLSearchParams to be defensive about both shapes.
  const search = useSearch();
  const [, setLocation] = useLocation();

  const [placedBy, setPlacedByState] = useState<PlacedByFilter>(() =>
    parsePlacedBy(new URLSearchParams(search).get(URL_PARAM)),
  );

  // Hydrate when the URL changes (e.g. user clicks a deep-link). We
  // intentionally don't sync the other direction here — that's done in
  // setPlacedBy below.
  useEffect(() => {
    const fromUrl = parsePlacedBy(
      new URLSearchParams(search).get(URL_PARAM),
    );
    setPlacedByState((cur) => (cur === fromUrl ? cur : fromUrl));
  }, [search]);

  const setPlacedBy = useCallback(
    (v: PlacedByFilter) => {
      setPlacedByState(v);
      // Mirror to URL (replace, not push) so reload + share works but
      // the back button isn't polluted.
      const params = new URLSearchParams(window.location.search);
      if (v === DEFAULT_FILTER) {
        params.delete(URL_PARAM);
      } else {
        params.set(URL_PARAM, v);
      }
      const qs = params.toString();
      const next = `${window.location.pathname}${qs ? `?${qs}` : ""}${
        window.location.hash
      }`;
      // wouter's setLocation accepts a path; pass replace:true so the
      // history entry is overwritten.
      setLocation(next, { replace: true });
    },
    [setLocation],
  );

  const value = useMemo<Ctx>(
    () => ({ placedBy, setPlacedBy }),
    [placedBy, setPlacedBy],
  );

  return (
    <PlacedByContext.Provider value={value}>
      {children}
    </PlacedByContext.Provider>
  );
}

/**
 * Read the current trade-provenance filter.
 *
 * Falls back to ``"all"`` (no filter) when called outside of a
 * `<PlacedByProvider>` so individual components can be reused on
 * pages that don't yet expose the toggle without a runtime crash.
 * The dashboard wraps everything in the provider, so production code
 * always gets the live value.
 */
export function usePlacedBy(): Ctx {
  const ctx = useContext(PlacedByContext);
  if (!ctx) {
    return {
      placedBy: DEFAULT_FILTER,
      setPlacedBy: () => undefined,
    };
  }
  return ctx;
}
