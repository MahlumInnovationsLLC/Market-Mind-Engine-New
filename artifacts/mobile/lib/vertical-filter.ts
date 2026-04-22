import { useQuery } from "@tanstack/react-query";
import type {
  MyAccess,
  VerticalVisibility,
} from "@workspace/api-client-react";
import { useEffect, useState } from "react";

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
