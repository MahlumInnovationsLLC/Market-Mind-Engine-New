import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "wouter";
import { ArrowRight, Radio, X } from "lucide-react";
import { apiFetch } from "@/lib/api";

type StuckSubscriber = {
  hub?: string | null;
  label?: string | null;
  consecutive_snapshots?: number | null;
  overflowed?: boolean | null;
};

type SnapshotCard = {
  key: string;
  status: "ok" | "degraded" | "down" | "unconfigured";
  extras?: Record<string, unknown> | null;
};

type HealthSnapshot = {
  cards: SnapshotCard[];
};

const SNOOZE_KEY = "axiom.stuckWsBanner.snoozedUntil";
// Match the server-side dedupe TTL default (1h) so dismissing the
// banner roughly mirrors the email dedupe window — long enough to stop
// being noisy, short enough that a still-stuck client comes back.
const SNOOZE_MS = 60 * 60 * 1000;

function readSnoozedKeys(): Record<string, number> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(SNOOZE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, number>;
    if (!parsed || typeof parsed !== "object") return {};
    const now = Date.now();
    const fresh: Record<string, number> = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof v === "number" && v > now) fresh[k] = v;
    }
    return fresh;
  } catch {
    return {};
  }
}

function writeSnoozedKeys(map: Record<string, number>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SNOOZE_KEY, JSON.stringify(map));
  } catch {
    /* quota / private mode — silently drop, banner just won't snooze */
  }
}

function subKey(sub: StuckSubscriber): string {
  return `${String(sub.hub ?? "?")}:${String(sub.label ?? "?")}`;
}

export function StuckWsBanner() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const [snoozed, setSnoozed] = useState<Record<string, number>>(() =>
    readSnoozedKeys(),
  );

  // Drop expired snoozes on a coarse interval so a still-stuck client
  // re-surfaces after the snooze elapses without needing a navigation.
  useEffect(() => {
    const id = window.setInterval(() => {
      setSnoozed((prev) => {
        const now = Date.now();
        const next: Record<string, number> = {};
        let changed = false;
        for (const [k, v] of Object.entries(prev)) {
          if (v > now) next[k] = v;
          else changed = true;
        }
        return changed ? next : prev;
      });
    }, 60_000);
    return () => window.clearInterval(id);
  }, []);

  const { data } = useQuery<HealthSnapshot>({
    queryKey: ["shell", "health-snapshot"],
    queryFn: () =>
      apiFetch<HealthSnapshot>("/api/health/snapshot", { getToken: token }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const stuck = useMemo<StuckSubscriber[]>(() => {
    const card = (data?.cards ?? []).find((c) => c.key === "ws_backpressure");
    const extras = card?.extras as Record<string, unknown> | undefined;
    const raw = extras?.stuck_subscribers;
    if (!Array.isArray(raw)) return [];
    return raw as StuckSubscriber[];
  }, [data]);

  const visible = useMemo(
    () => stuck.filter((s) => !(subKey(s) in snoozed)),
    [stuck, snoozed],
  );

  if (visible.length === 0) return null;

  const dismiss = () => {
    const until = Date.now() + SNOOZE_MS;
    const next = { ...snoozed };
    for (const s of visible) next[subKey(s)] = until;
    writeSnoozedKeys(next);
    setSnoozed(next);
  };

  const first = visible[0];
  const summary =
    visible.length === 1
      ? `${first.hub ?? "?"} · ${first.label ?? "?"}${
          first.overflowed ? " (overflowed)" : ""
        }`
      : `${visible.length} subscribers stuck (${first.hub ?? "?"} · ${
          first.label ?? "?"
        }${visible.length > 1 ? ", …" : ""})`;

  return (
    <div
      className="border-b border-warning/40 bg-warning/10 text-warning px-4 py-2 text-[10px] uppercase tracking-widest font-mono flex items-center gap-2"
      data-testid="banner-stuck-ws"
    >
      <Radio className="w-3.5 h-3.5 shrink-0" />
      <span className="font-bold">WS SUBSCRIBER STUCK</span>
      <span className="opacity-80 normal-case tracking-normal" data-testid="text-stuck-ws-summary">
        · {summary}
      </span>
      <Link
        href="/health"
        className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded border border-warning/50 hover:bg-warning/20 hover:text-warning focus:outline-none focus:ring-1 focus:ring-warning/60 transition-colors"
        data-testid="link-stuck-ws-health"
      >
        Open health
        <ArrowRight className="w-3 h-3" />
      </Link>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss stuck WebSocket banner"
        className="inline-flex items-center justify-center w-5 h-5 rounded border border-warning/50 hover:bg-warning/20 focus:outline-none focus:ring-1 focus:ring-warning/60 transition-colors"
        data-testid="button-dismiss-stuck-ws"
      >
        <X className="w-3 h-3" />
      </button>
    </div>
  );
}
