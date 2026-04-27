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
import {
  getGetMyAccessQueryKey,
  getSearchAllQueryKey,
  useGetMyAccess,
  useSearchAll,
} from "@workspace/api-client-react";

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

// The /api/search endpoint only returns `users` / `audit` results to admin
// callers, so a non-admin who searches for an email or audit-action substring
// will always see empty arrays even when an admin would have matched.  We use
// these heuristics to decide when an empty result might be worth flagging
// with a "some matches may be admin-only" hint — without showing it on every
// stray empty query.
const ADMIN_AUDIT_ACTION_PREFIXES = [
  "set_",
  "approve",
  "revoke",
  "grant",
  "promote",
  "demote",
];

function looksLikeAdminQuery(q: string): boolean {
  const v = q.trim().toLowerCase();
  if (v.length < 2) return false;
  // Email-ish: anything containing "@" is almost certainly an email lookup.
  if (v.includes("@")) return true;
  return ADMIN_AUDIT_ACTION_PREFIXES.some((p) => v.startsWith(p));
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

  // Used purely to gate the "some matches may be admin-only" hint below;
  // we never render the value itself, so the cached access record from
  // other pages is reused via the shared query key.
  const { data: access } = useGetMyAccess({
    query: {
      queryKey: getGetMyAccessQueryKey(),
      staleTime: 30_000,
    },
  });
  // Treat the user as non-admin for hint purposes only when we have an
  // explicit `is_admin: false` from the API; while access is still
  // loading we suppress the hint to avoid a flicker on admin sessions.
  const knownNonAdmin = access?.is_admin === false;

  function go(path: string) {
    setOpen(false);
    setQuery("");
    setLocation(path);
  }

  const trades = data?.trades ?? [];
  const debates = data?.debates ?? [];
  const cycles = data?.cycles ?? [];
  const users = data?.users ?? [];
  const audit = data?.audit ?? [];
  const billing = data?.billing ?? [];
  const hasResults =
    trades.length +
      debates.length +
      cycles.length +
      users.length +
      audit.length +
      billing.length >
    0;

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
          <>
            <CommandEmpty>NO_MATCHES</CommandEmpty>
            {knownNonAdmin && looksLikeAdminQuery(debounced) && (
              <div
                className="px-4 pb-4 text-center text-[11px] leading-snug text-muted-foreground"
                data-testid="search-admin-only-hint"
              >
                Some matches may be admin-only — ask an admin to search if you
                expected results.
              </div>
            )}
          </>
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

        {users.length > 0 && (
          <>
            {(trades.length > 0 || debates.length > 0 || cycles.length > 0) && (
              <CommandSeparator />
            )}
            <CommandGroup heading="Users">
              {users.map((u) => (
                <CommandItem
                  key={`user-${u.clerk_user_id}`}
                  value={`user-${u.clerk_user_id}-${u.email ?? ""}`}
                  onSelect={() =>
                    go(
                      `/system?tab=users&focus=${encodeURIComponent(
                        `user-${u.clerk_user_id}`,
                      )}`,
                    )
                  }
                  data-testid={`search-user-${u.clerk_user_id}`}
                >
                  <div className="flex w-full items-center justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-foreground">
                        {u.email || u.display_name || u.clerk_user_id}
                      </div>
                      <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                        {u.is_admin
                          ? "admin"
                          : u.approved
                            ? "approved"
                            : "pending"}
                        {" · "}last seen {fmtTime(u.last_seen_at)}
                      </div>
                    </div>
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground truncate max-w-[140px]">
                      {u.clerk_user_id}
                    </div>
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}

        {audit.length > 0 && (
          <>
            {(trades.length > 0 ||
              debates.length > 0 ||
              cycles.length > 0 ||
              users.length > 0) && <CommandSeparator />}
            <CommandGroup heading="Audit">
              {audit.map((a) => (
                <CommandItem
                  key={`audit-${a.id}`}
                  value={`audit-${a.id}-${a.action ?? ""}`}
                  onSelect={() =>
                    go(`/system?tab=audit&focus=audit-${a.id}`)
                  }
                  data-testid={`search-audit-${a.id}`}
                >
                  <div className="flex w-full items-center justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-foreground">
                        {a.action || `audit #${a.id}`}
                      </div>
                      <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                        {a.actor_email ?? a.actor_user_id ?? "—"}
                        {" → "}
                        {a.target_email ?? a.target_user_id ?? "—"}
                        {" · "}
                        {fmtTime(a.created_at)}
                      </div>
                    </div>
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                      #{a.id}
                    </div>
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}

        {billing.length > 0 && (
          <>
            {(trades.length > 0 ||
              debates.length > 0 ||
              cycles.length > 0 ||
              users.length > 0 ||
              audit.length > 0) && <CommandSeparator />}
            <CommandGroup heading="Billing">
              {billing.map((b, idx) => {
                if (b.kind === "event") {
                  const eventId = b.event_id ?? "";
                  return (
                    <CommandItem
                      key={`billing-event-${eventId || idx}`}
                      value={`billing-event-${eventId}-${b.event_type ?? ""}`}
                      onSelect={() =>
                        go(
                          `/system?tab=controls&focus=${encodeURIComponent(
                            `rc-event-${eventId}`,
                          )}`,
                        )
                      }
                      data-testid={`search-billing-event-${eventId || idx}`}
                    >
                      <div className="flex w-full items-center justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-foreground">
                            RC event {eventId || "—"}
                          </div>
                          <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                            {b.event_type ?? "—"}
                            {" · "}
                            {b.attempts ?? 0} tries
                            {" · "}
                            {fmtTime(b.last_failed_at)}
                            {b.last_error ? ` · ${b.last_error}` : ""}
                          </div>
                        </div>
                        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                          5xx
                        </div>
                      </div>
                    </CommandItem>
                  );
                }
                // kind === "customer"
                const provider = b.provider ?? "stripe";
                const clerkId = b.user_id ?? "";
                // RC mobile-only customers may not have a clerk id yet
                // (anonymous app_user_id). Fall back to the user-search
                // page for known clerk ids; otherwise stay on /system
                // settings so the operator can look them up by hand.
                const target = clerkId
                  ? `/system?tab=users&focus=${encodeURIComponent(
                      `user-${clerkId}`,
                    )}`
                  : `/system?tab=settings`;
                const key =
                  clerkId || b.app_user_id || b.email || `idx-${idx}`;
                return (
                  <CommandItem
                    key={`billing-customer-${provider}-${key}`}
                    value={`billing-customer-${provider}-${key}-${b.email ?? ""}`}
                    onSelect={() => go(target)}
                    data-testid={`search-billing-customer-${provider}-${key}`}
                  >
                    <div className="flex w-full items-center justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-foreground">
                          {b.email || clerkId || b.app_user_id || "—"}
                        </div>
                        <div className="truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                          {provider}
                          {" · "}
                          {b.tier ?? "—"}
                          {b.status ? ` · ${b.status}` : ""}
                          {" · updated "}
                          {fmtTime(b.updated_at)}
                        </div>
                      </div>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground truncate max-w-[140px]">
                        {clerkId || b.stripe_customer_id || b.app_user_id || ""}
                      </div>
                    </div>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </>
        )}
      </CommandList>
    </CommandDialog>
  );
}
