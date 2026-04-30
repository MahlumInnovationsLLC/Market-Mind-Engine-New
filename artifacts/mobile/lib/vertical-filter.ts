import { useQuery } from "@tanstack/react-query";
import type {
  MyAccess,
  VerticalsOverview,
  VerticalVisibility,
} from "@workspace/api-client-react";
import { useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";

let _selected: string[] = [];
const _listeners = new Set<(v: string[]) => void>();

function emit(): void {
  for (const l of _listeners) l(_selected);
}

export function getSelectedVerticals(): string[] {
  return _selected;
}

export function setSelectedVerticals(next: string[]): void {
  const norm = Array.from(new Set(next.map((v) => v.toLowerCase()))).sort();
  _selected = norm;
  emit();
}

export function toggleSelectedVertical(v: string): void {
  const lower = v.toLowerCase();
  setSelectedVerticals(
    _selected.includes(lower)
      ? _selected.filter((x) => x !== lower)
      : [..._selected, lower],
  );
}

export function clearSelectedVerticals(): void {
  setSelectedVerticals([]);
}

export function useSelectedVerticals(): {
  selected: string[];
  toggle: (v: string) => void;
  clear: () => void;
  matches: (v: string | null | undefined) => boolean;
  filterActive: boolean;
  hiddenActive: boolean;
} {
  const [selected, setSelected] = useState<string[]>(_selected);

  useEffect(() => {
    const sub = (next: string[]) => setSelected(next);
    _listeners.add(sub);
    return () => {
      _listeners.delete(sub);
    };
  }, []);

  const access = useQuery({
    queryKey: ["me", "access"],
    queryFn: () => apiFetch<MyAccess>("/api/me/access"),
    staleTime: 30_000,
  });
  const visibility = useQuery({
    queryKey: ["dashboard", "vertical-visibility"],
    queryFn: () =>
      apiFetch<VerticalVisibility>("/api/dashboard/vertical-visibility"),
    staleTime: 60_000,
  });

  const isAdmin = !!access.data?.is_admin;
  const hidden = new Set(
    (visibility.data?.hidden ?? []).map((v) => v.toLowerCase()),
  );
  const hiddenActive = !isAdmin && hidden.size > 0;
  const filterActive = selected.length > 0 || hiddenActive;

  const matches = (v: string | null | undefined) => {
    const lower = v ? v.toLowerCase() : null;
    if (!isAdmin && lower && hidden.has(lower)) return false;
    if (selected.length === 0) return true;
    if (!lower) return false;
    return selected.includes(lower);
  };

  return {
    selected,
    toggle: toggleSelectedVertical,
    clear: clearSelectedVerticals,
    matches,
    filterActive,
    hiddenActive,
  };
}

/**
 * Mirror of the web `useVerticalFilter().effectiveVerticals` derivation.
 *
 * Returns the lower-cased vertical slugs to send to a server-side filter
 * (e.g. the `?verticals=` CSV on `/api/public/tail-risk-exposure`).  An
 * empty array means "no narrowing — return everything".  Combines the
 * explicit chip selection with admin-managed hidden-vertical suppression
 * for non-admins so any tile that pushes verticals to the server never
 * leaks hidden P&L into a non-admin operator's headline.  Note: when
 * `hiddenActive` is true but the registry overview hasn't loaded yet this
 * returns `[]` for one render frame at worst (React Query dedupes the
 * in-flight `verticals-overview` request with the dashboard tab).
 */
export function useEffectiveVerticals(): string[] {
  const { selected } = useSelectedVerticals();

  const access = useQuery({
    queryKey: ["me", "access"],
    queryFn: () => apiFetch<MyAccess>("/api/me/access"),
    staleTime: 30_000,
  });
  const visibility = useQuery({
    queryKey: ["dashboard", "vertical-visibility"],
    queryFn: () =>
      apiFetch<VerticalVisibility>("/api/dashboard/vertical-visibility"),
    staleTime: 60_000,
  });
  const overview = useQuery({
    queryKey: ["dashboard", "verticals", 7],
    queryFn: () =>
      apiFetch<VerticalsOverview>("/api/dashboard/verticals?days=7"),
    staleTime: 60_000,
  });

  const isAdmin = !!access.data?.is_admin;
  const hidden = useMemo(
    () => new Set((visibility.data?.hidden ?? []).map((v) => v.toLowerCase())),
    [visibility.data?.hidden],
  );
  const knownVerticals = useMemo(
    () =>
      (overview.data?.verticals ?? [])
        .map((e) => (e.vertical ?? "").toLowerCase())
        .filter(Boolean),
    [overview.data?.verticals],
  );
  const hiddenActive = !isAdmin && hidden.size > 0;

  return useMemo(() => {
    if (selected.length > 0) {
      if (!isAdmin && hidden.size > 0) {
        return selected.filter((v) => !hidden.has(v));
      }
      return selected;
    }
    if (hiddenActive && knownVerticals.length > 0) {
      return knownVerticals.filter((v) => !hidden.has(v));
    }
    return [];
  }, [selected, isAdmin, hidden, hiddenActive, knownVerticals]);
}
