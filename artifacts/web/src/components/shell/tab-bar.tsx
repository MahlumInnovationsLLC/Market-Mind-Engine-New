import { useEffect, useState } from "react";
import { useLocation, useSearch } from "wouter";

export interface TabDef<K extends string> {
  key: K;
  label: string;
  subtitle?: string;
}

interface TabBarProps<K extends string> {
  tabs: TabDef<K>[];
  active: K;
  onSelect: (k: K) => void;
  ariaLabel: string;
  testIdPrefix: string;
}

/**
 * Cyberpunk-finance shared tab strip. Same visual language used on
 * /trading; lifted out so /intelligence (and future tabbed pages) stay
 * pixel-consistent without each page re-implementing the markup.
 */
export function TabBar<K extends string>({
  tabs,
  active,
  onSelect,
  ariaLabel,
  testIdPrefix,
}: TabBarProps<K>) {
  return (
    <div
      className="flex flex-wrap gap-1 self-start rounded-md border border-border bg-card p-1"
      role="tablist"
      aria-label={ariaLabel}
      data-testid={`tabs-${testIdPrefix}`}
    >
      {tabs.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onSelect(t.key)}
            className={`rounded px-4 py-1.5 text-xs uppercase tracking-widest transition-colors ${
              isActive
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
            data-testid={`tab-${testIdPrefix}-${t.key}`}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * URL-synced tab state. Mirrors the pattern used on /trading: state
 * lives in `?tab=` so back/forward navigation works and panels are
 * directly linkable.  The default tab key isn't written to the URL.
 */
export function useTabState<K extends string>(
  tabs: readonly K[],
  defaultKey: K,
  basePath: string,
  paramName = "tab",
): [K, (next: K) => void] {
  const search = useSearch();
  const [, setLocation] = useLocation();

  const parse = (qs: string): K => {
    const t = new URLSearchParams(qs).get(paramName);
    return tabs.includes(t as K) ? (t as K) : defaultKey;
  };

  const [tab, setTab] = useState<K>(() => parse(search));

  useEffect(() => {
    const next = parse(search);
    setTab((prev) => (prev === next ? prev : next));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const select = (next: K) => {
    setTab(next);
    const params = new URLSearchParams(search);
    if (next === defaultKey) params.delete(paramName);
    else params.set(paramName, next);
    const q = params.toString();
    setLocation(`${basePath}${q ? `?${q}` : ""}`, { replace: false });
  };

  return [tab, select];
}
