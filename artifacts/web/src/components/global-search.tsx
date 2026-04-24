import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { getSearchAllQueryKey, useSearchAll } from "@workspace/api-client-react";

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleString([], {
    hour12: false,
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(0)}%`;
}

export default function GlobalSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [, setLocation] = useLocation();

  // Cmd+K / Ctrl+K to open. Escape closes via Dialog default. We also
  // listen for a custom `axiom:open-search` event so the dashboard
  // header button can toggle the palette without dispatching a synthetic
  // keyboard event (which browsers may strip of "trusted" status).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    const onCustom = () => setOpen((v) => !v);
    window.addEventListener("keydown", onKey);
    window.addEventListener("axiom:open-search", onCustom);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("axiom:open-search", onCustom);
    };
  }, []);

  // Debounce + fire only when there's something to search.
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(query.trim()), 200);
    return () => window.clearTimeout(id);
  }, [query]);

  const searchParams = { q: debounced, limit: 10 };
  const { data, isFetching } = useSearchAll(searchParams, {
    query: {
      enabled: open && debounced.length >= 2,
      staleTime: 5_000,
      queryKey: getSearchAllQueryKey(searchParams),
    },
  });

  function go(path: string) {
    setOpen(false);
    setQuery("");
    setLocation(path);
  }

  const trades = data?.trades ?? [];
  const debates = data?.debates ?? [];
  const cycles = data?.cycles ?? [];
  const hasResults = trades.length + debates.length + cycles.length > 0;

  return (
    <CommandDialog open={open} onOpenChange={setOpen} shouldFilter={false}>
      <CommandInput
        placeholder="Search ticker, market id, or ISO date (YYYY-MM-DD)…"
        value={query}
        onValueChange={setQuery}
        data-testid="input-global-search"
      />
      <CommandList>
        {/* cmdk filters client-side by default; we override with our
            server-side hits, so disable the built-in filter via the
            `shouldFilter={false}` we pass on Command via the dialog
            wrapper.  CommandDialog forwards extra props to Command. */}
        {debounced.length < 2 ? (
          <div className="px-4 py-6 text-center text-xs uppercase tracking-widest text-muted-foreground">
            type at least 2 characters
          </div>
        ) : isFetching && !hasResults ? (
          <div className="px-4 py-6 text-center text-xs uppercase tracking-widest text-muted-foreground">
            searching…
          </div>
        ) : !hasResults ? (
          <CommandEmpty>NO_MATCHES</CommandEmpty>
        ) : null}

        {trades.length > 0 && (
          <CommandGroup heading="Trades">
            {trades.map((t) => (
              <CommandItem
                key={`trade-${t.id}`}
                value={`trade-${t.id}-${t.market_id ?? ""}`}
                onSelect={() => go(`/trading?focus=trade-${t.id}`)}
                data-testid={`search-trade-${t.id}`}
              >
                <div className="flex w-full items-center justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-foreground">
                      {t.market_title || t.market_id || `trade #${t.id}`}
                    </div>
                    <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                      {t.side ?? "—"} · {t.action ?? "—"} · {fmtTime(t.executed_at)}
                    </div>
                  </div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    #{t.id}
                  </div>
                </div>
              </CommandItem>
            ))}
          </CommandGroup>
        )}

        {debates.length > 0 && (
          <>
            {trades.length > 0 && <CommandSeparator />}
            <CommandGroup heading="Debates">
              {debates.map((d) => (
                <CommandItem
                  key={`debate-${d.id}`}
                  value={`debate-${d.id}-${d.matchup ?? ""}`}
                  onSelect={() => go(`/app?focus=debate-${d.id}`)}
                  data-testid={`search-debate-${d.id}`}
                >
                  <div className="flex w-full items-center justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-foreground">
                        {d.matchup || `debate #${d.id}`}
                      </div>
                      <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                        {d.league ?? "—"} · {d.recommended_action ?? "—"} ·
                        consensus {fmtPct(d.consensus_probability)} · {fmtTime(d.created_at)}
                      </div>
                    </div>
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                      #{d.id}
                    </div>
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}

        {cycles.length > 0 && (
          <>
            {(trades.length > 0 || debates.length > 0) && <CommandSeparator />}
            <CommandGroup heading="Cycles">
              {cycles.map((c) => (
                <CommandItem
                  key={`cycle-${c.id}`}
                  value={`cycle-${c.id}`}
                  onSelect={() => go(`/app?focus=cycle-${c.id}`)}
                  data-testid={`search-cycle-${c.id}`}
                >
                  <div className="flex w-full items-center justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-foreground">
                        cycle #{c.id} · {c.vertical ?? "—"}
                      </div>
                      <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                        {c.picks_evaluated ?? 0} evaluated · {c.trades_placed ?? 0} placed ·
                        {" "}{fmtTime(c.started_at)}
                      </div>
                    </div>
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}
      </CommandList>
    </CommandDialog>
  );
}
