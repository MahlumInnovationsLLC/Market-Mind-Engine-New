import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { useAuth } from "@clerk/react";
import { apiFetch, ApiError } from "@/lib/api";

type Card = {
  key: string;
  label: string;
  category: "live" | "passive" | "config";
  status: "ok" | "degraded" | "down" | "unconfigured";
  detail: string;
  checked_at: string | null;
  latency_ms: number | null;
  extras: Record<string, unknown>;
};

type Snapshot = {
  generated_at: string | null;
  duration_ms: number;
  uptime_seconds: number;
  cards: Card[];
  summary: {
    ok: number;
    degraded: number;
    down: number;
    unconfigured: number;
    errors_24h: number;
    overall: "ok" | "degraded" | "down";
  };
};

type ErrorsResponse = {
  items: { at: string; source: string; message: string }[];
  errors_24h: number;
};

type NotificationsStatus = {
  postmark: {
    configured: boolean;
    from_email: string;
    message_stream: string;
    recipients: string[];
  };
  recent_sends: {
    sent_at: number;
    to: string;
    subject: string;
    ok: boolean;
    error?: string | null;
    skipped_reason?: string | null;
  }[];
};

const STATUS_COLOR: Record<Card["status"], string> = {
  ok: "text-success border-success/40 bg-success/5",
  degraded: "text-warning border-warning/40 bg-warning/5",
  down: "text-destructive border-destructive/60 bg-destructive/10",
  unconfigured: "text-muted-foreground border-border bg-card",
};

const STATUS_DOT: Record<Card["status"], string> = {
  ok: "bg-success",
  degraded: "bg-warning",
  down: "bg-destructive",
  unconfigured: "bg-muted-foreground/40",
};

const ALERT_TYPES = [
  "halt",
  "drift",
  "tier_change",
  "live_trade",
  "daily_pnl_summary",
] as const;
type AlertType = (typeof ALERT_TYPES)[number];

function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function HealthPage() {
  const { getToken, isSignedIn } = useAuth();
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [errors, setErrors] = useState<ErrorsResponse | null>(null);
  const [notif, setNotif] = useState<NotificationsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<AlertType | null>(null);
  const [testResult, setTestResult] = useState<unknown>(null);
  const [dryRun, setDryRun] = useState(true);

  const token = useMemo(() => () => getToken().then((t) => t ?? null), [getToken]);

  async function refresh() {
    try {
      const [s, e, n] = await Promise.all([
        apiFetch<Snapshot>("/api/health/snapshot", { getToken: token }),
        apiFetch<ErrorsResponse>("/api/health/errors?limit=50", {
          getToken: token,
        }),
        apiFetch<NotificationsStatus>("/api/notifications/status", {
          getToken: token,
        }),
      ]);
      setSnap(s);
      setErrors(e);
      setNotif(n);
      setError(null);
    } catch (e: unknown) {
      const msg =
        e instanceof ApiError
          ? `HTTP ${e.status} :: ${e.message}`
          : e instanceof Error
            ? e.message
            : String(e);
      setError(msg);
    }
  }

  useEffect(() => {
    if (!isSignedIn) return;
    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSignedIn]);

  async function fireTest(type: AlertType) {
    setTesting(type);
    setTestResult(null);
    try {
      const res = await apiFetch("/api/notifications/test", {
        method: "POST",
        body: { type, dry_run: dryRun },
        getToken: token,
      });
      setTestResult(res);
    } catch (e: unknown) {
      setTestResult({
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setTesting(null);
    }
  }

  if (!isSignedIn) {
    return (
      <div className="min-h-[100dvh] bg-background text-muted-foreground font-mono flex items-center justify-center text-xs uppercase tracking-widest">
        AUTH :: sign in required
      </div>
    );
  }

  const grouped = useMemo(() => {
    const out: Record<Card["category"], Card[]> = {
      live: [],
      passive: [],
      config: [],
    };
    (snap?.cards ?? []).forEach((c) => out[c.category].push(c));
    return out;
  }, [snap]);

  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-mono p-6">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="flex items-baseline justify-between border-b border-border pb-4">
          <div>
            <div className="text-xs text-muted-foreground uppercase tracking-widest">
              AXIOM_PREDICT :: SYS_HEALTH
            </div>
            <h1 className="text-2xl font-bold uppercase tracking-tight text-primary">
              System Health
            </h1>
          </div>
          <nav className="flex gap-4 text-xs uppercase tracking-widest text-muted-foreground">
            <Link href="/" className="hover:text-primary">
              Dashboard
            </Link>
            <Link href="/pipeline" className="hover:text-primary">
              Pipeline
            </Link>
            <Link href="/billing" className="hover:text-primary">
              Billing
            </Link>
          </nav>
        </header>

        {error && (
          <div className="border border-destructive bg-destructive/10 text-destructive p-3 text-xs">
            {error}
          </div>
        )}

        {snap && (
          <section className="grid grid-cols-2 md:grid-cols-6 gap-3 text-xs uppercase tracking-widest">
            <SummaryStat
              label="Overall"
              value={snap.summary.overall}
              tone={snap.summary.overall}
            />
            <SummaryStat label="OK" value={String(snap.summary.ok)} tone="ok" />
            <SummaryStat
              label="Degraded"
              value={String(snap.summary.degraded)}
              tone={snap.summary.degraded > 0 ? "degraded" : "ok"}
            />
            <SummaryStat
              label="Down"
              value={String(snap.summary.down)}
              tone={snap.summary.down > 0 ? "down" : "ok"}
            />
            <SummaryStat label="Errors 24h" value={String(snap.summary.errors_24h)} />
            <SummaryStat label="Uptime" value={formatUptime(snap.uptime_seconds)} />
          </section>
        )}

        {snap && (
          <section className="text-[10px] text-muted-foreground uppercase tracking-widest">
            snapshot :: {snap.generated_at ?? "(pending)"} ::
            built in {snap.duration_ms}ms — auto-refreshes every 15s
          </section>
        )}

        {(["live", "passive", "config"] as const).map((cat) => (
          <section key={cat} className="space-y-3">
            <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
              {cat === "live"
                ? "Active probes"
                : cat === "passive"
                  ? "Background tasks"
                  : "Credentials"}
              <span className="ml-2 text-[10px]">
                ({grouped[cat].length})
              </span>
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {grouped[cat].map((c) => (
                <CardTile key={c.key} card={c} />
              ))}
              {grouped[cat].length === 0 && (
                <div className="text-xs text-muted-foreground">
                  (none registered)
                </div>
              )}
            </div>
          </section>
        ))}

        <section className="space-y-3">
          <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
            Recent errors (last 24h: {errors?.errors_24h ?? 0})
          </h2>
          <div className="border border-border bg-card max-h-72 overflow-y-auto text-xs">
            {(errors?.items ?? []).length === 0 ? (
              <div className="p-3 text-muted-foreground">no errors recorded</div>
            ) : (
              <table className="w-full">
                <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="text-left p-2">at</th>
                    <th className="text-left p-2">source</th>
                    <th className="text-left p-2">message</th>
                  </tr>
                </thead>
                <tbody>
                  {(errors?.items ?? []).map((e, i) => (
                    <tr key={i} className="border-b border-border/40">
                      <td className="p-2 align-top text-muted-foreground whitespace-nowrap">
                        {e.at}
                      </td>
                      <td className="p-2 align-top text-warning whitespace-nowrap">
                        {e.source}
                      </td>
                      <td className="p-2 align-top break-all">{e.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>

        <section className="space-y-3 border-t border-border pt-4">
          <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
            Notifications (Postmark)
          </h2>
          {notif && (
            <div className="text-xs space-y-1">
              <div>
                <span className="text-muted-foreground">configured</span> ::{" "}
                <span className={notif.postmark.configured ? "text-success" : "text-warning"}>
                  {notif.postmark.configured ? "yes" : "no (POSTMARK_SERVER_TOKEN missing)"}
                </span>
              </div>
              <div>
                <span className="text-muted-foreground">from</span> ::{" "}
                {notif.postmark.from_email}
              </div>
              <div>
                <span className="text-muted-foreground">recipients</span> ::{" "}
                {notif.postmark.recipients.length === 0
                  ? "(none — set OPERATOR_ALERT_EMAILS)"
                  : notif.postmark.recipients.join(", ")}
              </div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 text-xs">
            <label className="flex items-center gap-2 mr-3 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
              />
              <span className="text-muted-foreground uppercase tracking-widest">
                Dry run
              </span>
            </label>
            {ALERT_TYPES.map((t) => (
              <button
                key={t}
                onClick={() => fireTest(t)}
                disabled={testing !== null}
                className="border border-border bg-card hover:bg-muted px-3 py-1 uppercase tracking-widest disabled:opacity-50"
              >
                {testing === t ? "…" : `Test :: ${t}`}
              </button>
            ))}
          </div>

          {testResult !== null && (
            <pre className="text-[11px] bg-muted/30 border border-border p-3 overflow-x-auto whitespace-pre-wrap break-all">
              {JSON.stringify(testResult, null, 2)}
            </pre>
          )}

          {notif && notif.recent_sends.length > 0 && (
            <div className="border border-border bg-card max-h-60 overflow-y-auto text-xs">
              <table className="w-full">
                <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="text-left p-2">when</th>
                    <th className="text-left p-2">to</th>
                    <th className="text-left p-2">subject</th>
                    <th className="text-left p-2">result</th>
                  </tr>
                </thead>
                <tbody>
                  {notif.recent_sends.map((s, i) => (
                    <tr key={i} className="border-b border-border/40">
                      <td className="p-2 text-muted-foreground whitespace-nowrap">
                        {new Date(s.sent_at * 1000).toISOString().slice(11, 19)}
                      </td>
                      <td className="p-2 whitespace-nowrap">{s.to}</td>
                      <td className="p-2 break-all">{s.subject}</td>
                      <td className="p-2">
                        {s.ok ? (
                          <span className="text-success">sent</span>
                        ) : s.skipped_reason ? (
                          <span className="text-muted-foreground">
                            skipped: {s.skipped_reason}
                          </span>
                        ) : (
                          <span className="text-destructive">{s.error}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function SummaryStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "degraded" | "down";
}) {
  const toneCls =
    tone === "down"
      ? "text-destructive"
      : tone === "degraded"
        ? "text-warning"
        : tone === "ok"
          ? "text-success"
          : "text-foreground";
  return (
    <div className="border border-border bg-card p-3">
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div className={`text-lg font-bold ${toneCls}`}>{value}</div>
    </div>
  );
}

function CardTile({ card }: { card: Card }) {
  return (
    <div className={`border ${STATUS_COLOR[card.status]} p-3 text-xs space-y-1`}>
      <div className="flex items-center justify-between">
        <span className="font-bold uppercase tracking-widest">{card.label}</span>
        <span className="flex items-center gap-1 text-[10px] uppercase tracking-widest">
          <span className={`inline-block w-2 h-2 ${STATUS_DOT[card.status]}`} />
          {card.status}
        </span>
      </div>
      <div className="text-muted-foreground break-all">
        {card.detail || "(no detail)"}
      </div>
      <div className="flex items-center justify-between text-[10px] text-muted-foreground/70">
        <span>{card.checked_at ?? "(no check yet)"}</span>
        {card.latency_ms !== null && <span>{card.latency_ms}ms</span>}
      </div>
    </div>
  );
}

export default HealthPage;
