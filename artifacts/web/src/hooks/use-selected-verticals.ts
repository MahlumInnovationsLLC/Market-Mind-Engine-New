/**
 * URL-backed multi-select for filtering dashboard panels by vertical.
 *
 * State lives in the `verticals` query string (comma-separated, lower-cased).
 * Selecting nothing means "show all" — same as omitting the param entirely.
 *
 * Reads/writes via the History API + a `popstate` listener so it works
 * with `wouter` routing without forcing a navigation. Other hooks /
 * components subscribed to the same hook re-render synchronously through
 * a tiny module-level event bus.
 */
import { useCallback, useEffect, useState } from "react";

const EVENT = "axiom:selected-verticals-change";
const PARAM = "verticals";

function readFromUrl(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const sp = new URLSearchParams(window.location.search);
    const raw = sp.get(PARAM) ?? "";
    if (!raw) return [];
    return raw
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
  } catch {
    return [];
  }
}

function writeToUrl(values: string[]): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (values.length === 0) {
    url.searchParams.delete(PARAM);
  } else {
    url.searchParams.set(PARAM, values.join(","));
  }
  window.history.replaceState(null, "", url.toString());
  window.dispatchEvent(new Event(EVENT));
}

export interface SelectedVerticalsApi {
  selected: string[];
  isSelected: (v: string) => boolean;
  toggle: (v: string) => void;
  setAll: (vs: string[]) => void;
  clear: () => void;
  /** Convenience predicate. Returns true when no filter is active OR the
   * vertical is in the active set. Components use this to client-side
   * filter rows by `vertical` — hidden rows simply don't render. */
  matches: (v: string | null | undefined) => boolean;
}

export function useSelectedVerticals(): SelectedVerticalsApi {
  const [selected, setSelected] = useState<string[]>(() => readFromUrl());

  useEffect(() => {
    const sync = () => setSelected(readFromUrl());
    window.addEventListener(EVENT, sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener("popstate", sync);
    };
  }, []);

  const isSelected = useCallback((v: string) => selected.includes(v.toLowerCase()), [selected]);

  const toggle = useCallback(
    (v: string) => {
      const lower = v.toLowerCase();
      const next = selected.includes(lower)
        ? selected.filter((x) => x !== lower)
        : [...selected, lower];
      writeToUrl(next);
    },
    [selected],
  );

  const setAll = useCallback((vs: string[]) => {
    writeToUrl(Array.from(new Set(vs.map((v) => v.toLowerCase()))));
  }, []);

  const clear = useCallback(() => writeToUrl([]), []);

  const matches = useCallback(
    (v: string | null | undefined) => {
      if (selected.length === 0) return true;
      if (!v) return false;
      return selected.includes(v.toLowerCase());
    },
    [selected],
  );

  return { selected, isSelected, toggle, setAll, clear, matches };
}
