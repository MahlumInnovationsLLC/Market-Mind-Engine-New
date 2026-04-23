import { useEffect, useMemo, useState } from "react";
import { Redirect } from "wouter";
import { useAuth } from "@clerk/react";
import { apiFetch, ApiError } from "@/lib/api";

export default function HealthPage() {
  // /health has been folded into the unified /system page (Health tab).
  // The route is preserved for bookmarks and external links.
  return <Redirect to="/system?tab=health" />;
}

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

type ReadyCheck = {
  name: string;
  ok: boolean;
  detail: string;
  checked_at: number;
};

type ReadinessSnapshot = {
  status?: "ready" | "not_ready";
  ready: boolean;
  started_at: number | null;
  completed_at: number | null;
  elapsed_seconds: number | null;
  checks: Record<string, ReadyCheck>;
};

const READY_CHECK_ORDER = ["migrations", "secrets", "models", "feeds"] as const;

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
  dedupe_suppressions?: Record<string, number>;
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

type ModelAlert = {
  id: number;
  vertical: string;
  alert_type: string;
  severity: string;
  metric_name: string;
  metric_value: number | null;
  threshold: number | null;
  message: string;
  acknowledged: boolean;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  snooze_until: string | null;
  created_at: string | null;
  age_seconds: number;
};

type ModelAlertsResponse = {
  items: ModelAlert[];
  count: number;
};

const ALERT_TYPES = [
  "halt",
  "drift",
  "tier_change",
  "live_trade",
  "daily_pnl_summary",
] as const;
type AlertType = (typeof ALERT_TYPES)[number];

function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 24) return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh > 0 ? `${d}d ${rh}h` : `${d}d`;
}

function formatSnoozeLabel(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / 1440)}d`;
}

function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function HealthPanel() {
  const { getToken, isSignedIn } = useAuth();
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [readiness, setReadiness] = useState<ReadinessSnapshot | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [errors, setErrors] = useState<ErrorsResponse | null>(null);
  const [notif, setNotif] = useState<NotificationsStatus | null>(null);
  const [modelAlerts, setModelAlerts] = useState<ModelAlertsResponse | null>(null);
  const [alertView, setAlertView] = useState<"open" | "all">("open");
  const [alertBusy, setAlertBusy] = useState<number | null>(null);
  const [alertError, setAlertError] = useState<string | null>(null);
  const [snoozeMinutes, setSnoozeMinutes] = useState<number>(60);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<AlertType | null>(null);
  const [testResult, setTestResult] = useState<unknown>(null);
  const [dryRun, setDryRun] = useState(true);

  const token = useMemo(() => () => getToken().then((t) => t ?? null), [getToken]);

  const alertsPath = (view: "open" | "all") =>
    view === "all"
      ? "/api/health/model-alerts?ack=true"
      : "/api/health/model-alerts?ack=false";

  async function refreshReadiness() {
    try {
      const r = await apiFetch<ReadinessSnapshot>("/api/health/ready");
      setReadiness(r);
      setReadinessError(null);
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 503) {
        const body = e.body as { detail?: ReadinessSnapshot } | null;
        if (body && body.detail && typeof body.detail === "object") {
          setReadiness(body.detail);
          setReadinessError(null);
          return;
        }
      }
      setReadinessError(
        e instanceof ApiError
          ? `HTTP ${e.status} :: ${e.message}`
          : e instanceof Error
            ? e.message
            : String(e),
      );
    }
  }

  async function refresh() {
    refreshReadiness();
    try {
      const [s, e, n, m] = await Promise.all([
        apiFetch<Snapshot>("/api/health/snapshot", { getToken: token }),
        apiFetch<ErrorsResponse>("/api/health/errors?limit=50", {
          getToken: token,
        }),
        apiFetch<NotificationsStatus>("/api/notifications/status", {
          getToken: token,
        }),
        apiFetch<ModelAlertsResponse>(alertsPath(alertView), {
          getToken: token,
        }),
      ]);
      setSnap(s);
      setErrors(e);
      setNotif(n);
      setModelAlerts(m);
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
  }, [isSignedIn, alertView]);

  async function ackAlert(id: number, snoozeMinutes: number | null) {
    setAlertBusy(id);
    setAlertError(null);
    try {
      await apiFetch(`/api/health/model-alerts/${id}/ack`, {
        method: "POST",
        body: snoozeMinutes ? { snooze_minutes: snoozeMinutes } : {},
        getToken: token,
      });
      const m = await apiFetch<ModelAlertsResponse>(
        alertsPath(alertView),
        { getToken: token },
      );
      setModelAlerts(m);
    } catch (e: unknown) {
      setAlertError(
        e instanceof ApiError
          ? `HTTP ${e.status} :: ${e.message}`
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setAlertBusy(null);
    }
  }

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

  const grouped = useMemo(() => {
    const out: Record<Card["category"], Card[]> = {
      live: [],
      passive: [],
      config: [],
    };
    (snap?.cards ?? []).forEach((c) => out[c.category].push(c));
    return out;
  }, [snap]);

  if (!isSignedIn) {
    return (
      <div className="text-xs text-muted-foreground uppercase tracking-widest">
        AUTH :: sign in required
      </div>
    );
  }

  return (
    <div className="space-y-6 font-mono" data-testid="panel-health">
        <ReadinessSection
          snap={readiness}
          error={readinessError}
        />

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
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
              {alertView === "all"
                ? "Drift alerts (recent audit log)"
                : "Open model drift alerts"}
              <span className="ml-2 text-[10px]">
                ({modelAlerts?.count ?? 0})
              </span>
            </h2>
            <div className="flex items-center gap-3 flex-wrap">
              <div
                className="inline-flex border border-border bg-card text-[10px] uppercase tracking-widest"
                role="tablist"
                aria-label="Drift alerts view"
                data-testid="alert-view-toggle"
              >
                <button
                  role="tab"
                  aria-selected={alertView === "open"}
                  onClick={() => setAlertView("open")}
                  className={`px-2 py-1 ${
                    alertView === "open"
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:bg-muted/40"
                  }`}
                  data-testid="alert-view-open"
                >
                  Open
                </button>
                <button
                  role="tab"
                  aria-selected={alertView === "all"}
                  onClick={() => setAlertView("all")}
                  className={`px-2 py-1 border-l border-border ${
                    alertView === "all"
                      ? "bg-muted text-foreground"
                      : "text-muted-foreground hover:bg-muted/40"
                  }`}
                  data-testid="alert-view-all"
                >
                  All recent
                </button>
              </div>
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground/70 uppercase tracking-widest">
                <span>ack to clear · snooze hides for</span>
                <select
                  value={snoozeMinutes}
                  onChange={(e) => setSnoozeMinutes(Number(e.target.value))}
                  data-testid="select-snooze-duration"
                  className="border border-border bg-card px-1 py-0.5 uppercase tracking-widest text-[10px]"
                >
                  <option value={15}>15m</option>
                  <option value={60}>1h</option>
                  <option value={240}>4h</option>
                  <option value={1440}>24h</option>
                </select>
              </div>
            </div>
          </div>
          {alertError && (
            <div className="border border-destructive bg-destructive/10 text-destructive p-2 text-xs">
              {alertError}
            </div>
          )}
          <div className="border border-border bg-card max-h-72 overflow-y-auto text-xs">
            {(modelAlerts?.items ?? []).length === 0 ? (
              <div className="p-3 text-muted-foreground">
                {alertView === "all"
                  ? "no recent drift alerts"
                  : "no open drift alerts"}
              </div>
            ) : (
              <table className="w-full">
                <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  <tr className="border-b border-border">
                    <th className="text-left p-2">sev</th>
                    <th className="text-left p-2">status</th>
                    <th className="text-left p-2">vertical</th>
                    <th className="text-left p-2">metric</th>
                    <th className="text-left p-2">value</th>
                    <th className="text-left p-2">threshold</th>
                    <th className="text-left p-2">age</th>
                    <th className="text-left p-2">message</th>
                    <th className="text-right p-2">actions</th>
                  </tr>
                </thead>
                <tbody>
                  {(modelAlerts?.items ?? []).map((a) => {
                    const snoozedActive =
                      !a.acknowledged &&
                      a.snooze_until !== null &&
                      new Date(a.snooze_until).getTime() > Date.now();
                    const isOpen = !a.acknowledged && !snoozedActive;
                    return (
                      <tr
                        key={a.id}
                        className="border-b border-border/40 align-top"
                        data-testid={`alert-row-${a.id}`}
                      >
                        <td className="p-2 whitespace-nowrap">
                          <span
                            className={
                              a.severity === "critical"
                                ? "text-destructive uppercase tracking-widest"
                                : "text-warning uppercase tracking-widest"
                            }
                          >
                            {a.severity}
                          </span>
                        </td>
                        <td className="p-2 whitespace-nowrap text-[10px] uppercase tracking-widest">
                          {a.acknowledged ? (
                            <div className="text-success">
                              <div>acked</div>
                              <div className="text-muted-foreground normal-case tracking-normal">
                                {a.acknowledged_by ?? "—"}
                              </div>
                              <div className="text-muted-foreground normal-case tracking-normal">
                                {a.acknowledged_at ?? "—"}
                              </div>
                            </div>
                          ) : snoozedActive ? (
                            <div className="text-warning">
                              <div>snoozed</div>
                              <div className="text-muted-foreground normal-case tracking-normal">
                                until {a.snooze_until}
                              </div>
                            </div>
                          ) : (
                            <span className="text-destructive">open</span>
                          )}
                        </td>
                        <td className="p-2 whitespace-nowrap">{a.vertical}</td>
                        <td className="p-2 whitespace-nowrap text-muted-foreground">
                          {a.metric_name}
                        </td>
                        <td className="p-2 whitespace-nowrap">
                          {a.metric_value !== null
                            ? a.metric_value.toFixed(3)
                            : "—"}
                        </td>
                        <td className="p-2 whitespace-nowrap text-muted-foreground">
                          {a.threshold !== null ? a.threshold.toFixed(3) : "—"}
                        </td>
                        <td className="p-2 whitespace-nowrap text-muted-foreground">
                          {formatAge(a.age_seconds)}
                        </td>
                        <td className="p-2 break-all">{a.message}</td>
                        <td className="p-2 text-right whitespace-nowrap">
                          {isOpen ? (
                            <div className="inline-flex gap-1">
                              <button
                                onClick={() => ackAlert(a.id, null)}
                                disabled={alertBusy !== null}
                                className="border border-border bg-card hover:bg-muted px-2 py-1 uppercase tracking-widest text-[10px] disabled:opacity-50"
                              >
                                {alertBusy === a.id ? "…" : "Ack"}
                              </button>
                              <button
                                onClick={() => ackAlert(a.id, snoozeMinutes)}
                                disabled={alertBusy !== null}
                                data-testid={`button-snooze-${a.id}`}
                                className="border border-border bg-card hover:bg-muted px-2 py-1 uppercase tracking-widest text-[10px] disabled:opacity-50"
                              >
                                {alertBusy === a.id
                                  ? "…"
                                  : `Snooze ${formatSnoozeLabel(snoozeMinutes)}`}
                              </button>
                            </div>
                          ) : (
                            <span className="text-[10px] text-muted-foreground/60 uppercase tracking-widest">
                              —
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </section>

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

          {notif && notif.dedupe_suppressions && (
            <div className="border border-border bg-card p-3 text-xs">
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
                dedupe :: suppressed since boot
              </div>
              {Object.keys(notif.dedupe_suppressions).length === 0 ? (
                <div className="text-muted-foreground">
                  no duplicates suppressed
                </div>
              ) : (
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
                  {Object.entries(notif.dedupe_suppressions)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([alertType, count]) => (
                      <div
                        key={alertType}
                        className="border border-border/60 bg-muted/20 p-2"
                      >
                        <div className="text-[10px] text-muted-foreground uppercase tracking-widest break-all">
                          {alertType}
                        </div>
                        <div className="text-base font-bold">{count}</div>
                      </div>
                    ))}
                </div>
              )}
            </div>
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
  );
}

function formatElapsed(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

function ReadinessSection({
  snap,
  error,
}: {
  snap: ReadinessSnapshot | null;
  error: string | null;
}) {
  const checks = snap?.checks ?? {};
  const known = READY_CHECK_ORDER.filter((n) => n in checks);
  const extras = Object.keys(checks).filter(
    (n) => !(READY_CHECK_ORDER as readonly string[]).includes(n),
  );
  const orderedNames = [...known, ...extras];
  const failing = orderedNames.filter((n) => checks[n] && !checks[n].ok);
  const ready = !!snap?.ready;
  const badgeCls = ready
    ? "border-success text-success bg-success/10"
    : "border-destructive text-destructive bg-destructive/10";
  const badgeLabel = snap === null ? "PENDING" : ready ? "READY" : "NOT READY";

  return (
    <section
      className="border border-border bg-card p-4 space-y-3"
      data-testid="panel-readiness"
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <span
            className={`inline-flex items-center px-3 py-1 border text-xs font-bold uppercase tracking-widest ${badgeCls}`}
            data-testid="readiness-badge"
          >
            {badgeLabel}
          </span>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            ready gate ::{" "}
            {snap
              ? ready
                ? `flipped after ${formatElapsed(snap.elapsed_seconds)}`
                : `waiting · ${formatElapsed(snap.elapsed_seconds)} since boot`
              : "loading…"}
          </span>
        </div>
        {!ready && failing.length > 0 && (
          <span
            className="text-[10px] uppercase tracking-widest text-destructive"
            data-testid="readiness-failing"
          >
            failing :: {failing.join(", ")}
          </span>
        )}
      </div>

      {error && (
        <div className="border border-destructive bg-destructive/10 text-destructive p-2 text-xs">
          readiness probe error :: {error}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {orderedNames.length === 0 && (
          <div className="text-xs text-muted-foreground col-span-full">
            (no checks reported yet)
          </div>
        )}
        {orderedNames.map((name) => {
          const c = checks[name];
          const isFailing = !ready && !c.ok;
          const tone = c.ok
            ? "border-success/50 bg-success/5 text-success"
            : "border-destructive/60 bg-destructive/10 text-destructive";
          const checkedAt = c.checked_at
            ? new Date(c.checked_at * 1000).toISOString().slice(11, 19)
            : "—";
          return (
            <div
              key={name}
              data-testid={`readiness-check-${name}`}
              className={`border p-2 text-xs space-y-1 ${tone} ${
                isFailing ? "ring-2 ring-destructive" : ""
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-bold uppercase tracking-widest">
                  {name}
                </span>
                <span className="text-[10px] uppercase tracking-widest">
                  {c.ok ? "ok" : "fail"}
                </span>
              </div>
              <div className="text-muted-foreground break-all">
                {c.detail || "(no detail)"}
              </div>
              <div className="text-[10px] text-muted-foreground/70">
                checked {checkedAt}
              </div>
            </div>
          );
        })}
      </div>
    </section>
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
  const isFeed = card.key.startsWith("feed:");
  const lastPull =
    isFeed && card.extras && typeof card.extras === "object"
      ? ((card.extras as Record<string, unknown>).last_pull as string | null) ?? null
      : null;
  const errorRate =
    isFeed && card.extras && typeof card.extras === "object"
      ? ((card.extras as Record<string, unknown>).error_rate as number | null) ?? null
      : null;
  const polls =
    isFeed && card.extras && typeof card.extras === "object"
      ? ((card.extras as Record<string, unknown>).poll_count as number | null) ?? null
      : null;
  const fails =
    isFeed && card.extras && typeof card.extras === "object"
      ? ((card.extras as Record<string, unknown>).failure_count as number | null) ?? null
      : null;
  return (
    <div className={`border ${STATUS_COLOR[card.status]} p-3 text-xs space-y-1`}>
      <div className="flex items-center justify-between">
        <span className="font-bold uppercase tracking-widest">{card.label}</span>
        <span className="flex items-center gap-1 text-[10px] uppercase tracking-widest">
          <span className={`inline-block w-2 h-2 ${STATUS_DOT[card.status]}`} />
          {card.status}
        </span>
      </div>
      {isFeed && (
        <div
          className="grid grid-cols-2 gap-1 text-[10px] uppercase tracking-widest text-muted-foreground"
          data-testid={`feed-metrics-${card.key}`}
        >
          <span>
            last pull:{" "}
            <span className="text-foreground normal-case tracking-normal">
              {lastPull ?? "never"}
            </span>
          </span>
          <span>
            err rate:{" "}
            <span className="text-foreground normal-case tracking-normal">
              {errorRate != null ? `${(errorRate * 100).toFixed(1)}%` : "—"}
              {polls != null && fails != null ? ` (${fails}/${polls})` : ""}
            </span>
          </span>
        </div>
      )}
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

