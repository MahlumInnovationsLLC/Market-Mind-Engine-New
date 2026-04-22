import { useEffect, useState } from "react";

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
} {
  const [selected, setSelected] = useState<string[]>(_selected);

  useEffect(() => {
    const sub = (next: string[]) => setSelected(next);
    _listeners.add(sub);
    return () => {
      _listeners.delete(sub);
    };
  }, []);

  const matches = (v: string | null | undefined) => {
    if (selected.length === 0) return true;
    if (!v) return false;
    return selected.includes(v.toLowerCase());
  };

  return {
    selected,
    toggle: toggleSelectedVertical,
    clear: clearSelectedVerticals,
    matches,
  };
}
