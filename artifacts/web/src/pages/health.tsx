import { useEffect, useMemo, useState } from "react";
import { Redirect } from "wouter";
import { useAuth } from "@clerk/react";
import { useGetMyAccess } from "@workspace/api-client-react";
import { apiFetch, ApiError, formatErrorMessage } from "@/lib/api";

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

type DailySummaryPreview = {
  date_utc: string;
  is_today: boolean;
  subject: string | null;
  body_preview: string | null;
  html_preview: string | null;
  generated_at: string;
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
  const [previewDate, setPreviewDate] = useState<string>(() =>
    new Date().toISOString().slice(0, 10),
  );
  const [preview, setPreview] = useState<DailySummaryPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

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
        formatErrorMessage(
          e,
          e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
        ),
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
      const msg = formatErrorMessage(
        e,
        e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
      );
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
        formatErrorMessage(
          e,
          e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
        ),
      );
    } finally {
      setAlertBusy(null);
    }
  }

  async function loadPreview(date: string) {
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const params = new URLSearchParams({ date });
      const res = await apiFetch<DailySummaryPreview>(
        `/api/notifications/daily-summary/preview?${params.toString()}`,
        { getToken: token },
      );
      setPreview(res);
    } catch (e: unknown) {
      setPreview(null);
      setPreviewError(
        formatErrorMessage(
          e,
          e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
        ),
      );
    } finally {
      setPreviewLoading(false);
    }
  }

  // Auto-load today's preview the first time the panel mounts so
  // operators don't have to click "Load" just to see the current
  // draft. Re-loads are explicit (button) so we don't re-render the
  // sandboxed iframe on every 15s refresh tick.
  useEffect(() => {
    if (!isSignedIn) return;
    if (preview !== null || previewLoading) return;
    loadPreview(previewDate);
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
        error: formatErrorMessage(e),
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
    // The crypto freshness card is hoisted into its own dedicated
    // widget above the grid so it isn't duplicated as a generic tile.
    (snap?.cards ?? [])
      .filter((c) => c.key !== "crypto_feed_freshness")
      .forEach((c) => out[c.category].push(c));
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

        {snap?.cards
          .filter((c) => c.key === "crypto_feed_freshness")
          .map((c) => (
            <CryptoFreshnessWidget key={c.key} card={c} />
          ))}

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

        <KalshiCryptoCandidatesPanel token={token} />

        <CryptoAccuracyPanel token={token} />


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

        <section
          className="space-y-3 border-t border-border pt-4"
          data-testid="panel-daily-summary-preview"
        >
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
              Daily summary preview
            </h2>
            {preview && (
              <span className="text-[10px] uppercase tracking-widest text-muted-foreground/70">
                rendered {preview.generated_at}
              </span>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">
            Re-renders the exact HTML email subscribers would receive for the
            chosen UTC date. Pick a past day to inspect a historical send;
            today reflects the live draft the scheduler will fire next.
          </p>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <label
              htmlFor="daily-summary-preview-date"
              className="text-muted-foreground uppercase tracking-widest"
            >
              Date (UTC)
            </label>
            <input
              id="daily-summary-preview-date"
              type="date"
              value={previewDate}
              max={new Date().toISOString().slice(0, 10)}
              onChange={(e) => setPreviewDate(e.target.value)}
              data-testid="input-preview-date"
              className="border border-border bg-card px-2 py-1 font-mono"
            />
            <button
              onClick={() => loadPreview(previewDate)}
              disabled={previewLoading || !previewDate}
              data-testid="button-load-preview"
              className="border border-border bg-card hover:bg-muted px-3 py-1 uppercase tracking-widest disabled:opacity-50"
            >
              {previewLoading ? "Loading…" : "Load"}
            </button>
            <button
              onClick={() => {
                const today = new Date().toISOString().slice(0, 10);
                setPreviewDate(today);
                loadPreview(today);
              }}
              disabled={previewLoading}
              data-testid="button-preview-today"
              className="border border-border bg-card hover:bg-muted px-3 py-1 uppercase tracking-widest disabled:opacity-50"
            >
              Today
            </button>
          </div>

          {previewError && (
            <div
              className="border border-destructive bg-destructive/10 text-destructive p-2 text-xs"
              data-testid="preview-error"
            >
              {previewError}
            </div>
          )}

          {preview && (
            <div className="space-y-2">
              <div className="text-[11px] text-muted-foreground space-y-0.5">
                <div>
                  <span className="uppercase tracking-widest">date</span> ::{" "}
                  <span className="font-mono text-foreground">
                    {preview.date_utc}
                  </span>
                  {preview.is_today && (
                    <span className="ml-2 text-[10px] uppercase tracking-widest text-accent-cyan/80">
                      today
                    </span>
                  )}
                </div>
                <div className="break-all">
                  <span className="uppercase tracking-widest">subject</span> ::{" "}
                  <span className="font-mono text-foreground">
                    {preview.subject ?? "(no subject)"}
                  </span>
                </div>
              </div>
              {preview.html_preview ? (
                <iframe
                  title="Daily summary HTML preview"
                  data-testid="iframe-daily-summary-preview"
                  sandbox=""
                  srcDoc={preview.html_preview}
                  className="w-full border border-border bg-white"
                  style={{ height: "640px" }}
                />
              ) : (
                <div
                  className="border border-border bg-card p-3 text-xs text-muted-foreground"
                  data-testid="preview-empty"
                >
                  No HTML body was rendered for this date.
                </div>
              )}
            </div>
          )}

          {!preview && !previewError && !previewLoading && (
            <div className="text-xs text-muted-foreground">
              No preview loaded yet — pick a date and click Load.
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

// URL the "Not enforced" / "Invalid" manifest signature badge links
// to. Points at the section in the retrain runbook that explains how
// to set `AXIOM_MANIFEST_PUBLIC_KEY` so operators have a self-serve
// path to enforce signature verification on a deployment.
const MANIFEST_SIGNATURE_DOCS_URL =
  "/docs/retrain_runbook.md#manifest-signature-enforcement";

function ManifestSignatureBadge({ extras }: { extras: Record<string, unknown> }) {
  const sig = extras.manifest_signature;
  // Loader hasn't reported yet (boot in progress, or pre-manifest
  // versions of the API). Render nothing rather than guessing.
  if (sig !== "ok" && sig !== "skipped" && sig !== "invalid") return null;

  if (sig === "ok") {
    return (
      <span
        className="inline-flex items-center gap-1 px-2 py-0.5 border border-success/60 bg-success/10 text-success text-[10px] uppercase tracking-widest"
        data-testid="manifest-signature-badge"
        data-signature="ok"
        title="Manifest ed25519 signature verified against AXIOM_MANIFEST_PUBLIC_KEY."
      >
        <span className="inline-block w-1.5 h-1.5 bg-success" />
        sig: verified
      </span>
    );
  }

  if (sig === "skipped") {
    return (
      <a
        href={MANIFEST_SIGNATURE_DOCS_URL}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 px-2 py-0.5 border border-muted-foreground/40 bg-muted/30 text-muted-foreground hover:text-foreground hover:bg-muted/50 text-[10px] uppercase tracking-widest"
        data-testid="manifest-signature-badge"
        data-signature="skipped"
        title="AXIOM_MANIFEST_PUBLIC_KEY is not set — signature checks are skipped. Click for docs."
      >
        <span className="inline-block w-1.5 h-1.5 bg-muted-foreground/60" />
        sig: not enforced
      </a>
    );
  }

  // sig === "invalid" — this is loud on purpose. Show the underlying
  // error so the operator can tell tampering ("ed25519 verification
  // failed") apart from a transient download problem.
  const err =
    typeof extras.manifest_error === "string" && extras.manifest_error
      ? extras.manifest_error
      : "signature verification failed";
  return (
    <a
      href={MANIFEST_SIGNATURE_DOCS_URL}
      target="_blank"
      rel="noreferrer"
      className="inline-flex flex-col items-start gap-0.5 px-2 py-0.5 border border-destructive bg-destructive/15 text-destructive hover:bg-destructive/25 text-[10px] uppercase tracking-widest max-w-full"
      data-testid="manifest-signature-badge"
      data-signature="invalid"
      title={err}
    >
      <span className="flex items-center gap-1">
        <span className="inline-block w-1.5 h-1.5 bg-destructive" />
        sig: invalid
      </span>
      <span
        className="normal-case tracking-normal break-all max-w-[220px] line-clamp-2"
        data-testid="manifest-signature-error"
      >
        {err}
      </span>
    </a>
  );
}

// Default cadence the auto-probe loop runs at when the API hasn't told
// us otherwise (matches AXIOM_KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS).
// Used only as a safety net so the "stale" badge still appears on
// older API builds that don't surface ``probe_interval_seconds``.
const KALSHI_CLUSTER_DEFAULT_PROBE_INTERVAL_SECONDS = 600;

function KalshiClusterDetails({
  extras,
}: {
  extras: Record<string, unknown>;
}) {
  const checked = extras.checked === true;
  const configuredEnv =
    typeof extras.configured_env === "string" ? extras.configured_env : null;
  const keyBelongsTo =
    typeof extras.key_belongs_to === "string" ? extras.key_belongs_to : null;
  // The probe writes "key_belongs_to: <other env>" only when the keys
  // verifiably authenticate against the wrong cluster. Highlight that
  // mismatch loudly so the operator can fix KALSHI_ENV in one glance.
  const mismatch =
    checked &&
    configuredEnv !== null &&
    keyBelongsTo !== null &&
    keyBelongsTo !== configuredEnv;

  // "last checked Xm ago" — answers "is this status fresh, or a stuck
  // verdict from a wedged probe loop?". Prefer the epoch the probe
  // recorded itself; fall back to parsing the ISO-8601 string.
  const checkedAtEpoch =
    typeof extras.checked_at_epoch === "number"
      ? extras.checked_at_epoch * 1000
      : typeof extras.checked_at === "string"
        ? Date.parse(extras.checked_at)
        : NaN;
  const intervalSeconds =
    typeof extras.probe_interval_seconds === "number" &&
    extras.probe_interval_seconds > 0
      ? extras.probe_interval_seconds
      : KALSHI_CLUSTER_DEFAULT_PROBE_INTERVAL_SECONDS;
  const ageSeconds = Number.isFinite(checkedAtEpoch)
    ? Math.max(0, Math.floor((Date.now() - checkedAtEpoch) / 1000))
    : null;
  // Anything older than ~3x the configured cadence means the re-probe
  // loop has almost certainly stopped firing — surface it so a green
  // status doesn't hide a wedged probe.
  const stale = ageSeconds !== null && ageSeconds > intervalSeconds * 3;

  return (
    <div className="space-y-1" data-testid="kalshi-cluster-details">
      <div className="grid grid-cols-2 gap-1 text-[10px] uppercase tracking-widest">
        <span>
          configured_env:{" "}
          <span
            className="text-foreground normal-case tracking-normal"
            data-testid="kalshi-cluster-configured-env"
          >
            {configuredEnv ?? "—"}
          </span>
        </span>
        <span>
          key_belongs_to:{" "}
          <span
            className={`normal-case tracking-normal ${
              mismatch ? "text-destructive font-bold" : "text-foreground"
            }`}
            data-testid="kalshi-cluster-key-belongs-to"
          >
            {keyBelongsTo ?? (checked ? "unknown" : "—")}
          </span>
        </span>
      </div>
      {checked && ageSeconds !== null && (
        <div className="text-[10px] uppercase tracking-widest">
          last checked:{" "}
          <span
            className={`normal-case tracking-normal ${
              stale ? "text-warning font-bold" : "text-foreground"
            }`}
            data-testid="kalshi-cluster-last-checked"
            data-stale={stale ? "true" : "false"}
            title={
              typeof extras.checked_at === "string" ? extras.checked_at : ""
            }
          >
            {formatAge(ageSeconds)} ago
          </span>
          {stale && (
            <span
              className="ml-2 text-warning"
              data-testid="kalshi-cluster-stale-warning"
            >
              · stale (re-probe loop may be stuck)
            </span>
          )}
        </div>
      )}
    </div>
  );
}

type CryptoPair = {
  symbol: string;
  granularity: string;
  expected_seconds: number;
  max_ts: string | null;
  age_seconds: number | null;
  yellow_threshold_seconds: number;
  red_threshold_seconds: number;
  status: "ok" | "degraded" | "down";
};

const PAIR_STATUS_RANK: Record<CryptoPair["status"], number> = {
  down: 0,
  degraded: 1,
  ok: 2,
};

const PAIR_STATUS_TEXT: Record<CryptoPair["status"], string> = {
  ok: "text-success",
  degraded: "text-warning",
  down: "text-destructive",
};

function formatPairAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  return formatAge(Math.floor(seconds));
}

export function CryptoFreshnessWidget({ card }: { card: Card }) {
  const extras =
    card.extras && typeof card.extras === "object"
      ? (card.extras as Record<string, unknown>)
      : {};
  const rawPairs = Array.isArray(extras.pairs)
    ? (extras.pairs as unknown[])
    : [];
  const pairs: CryptoPair[] = rawPairs.filter(
    (p): p is CryptoPair =>
      !!p &&
      typeof p === "object" &&
      typeof (p as CryptoPair).symbol === "string" &&
      typeof (p as CryptoPair).granularity === "string" &&
      typeof (p as CryptoPair).status === "string",
  );
  if (pairs.length === 0) return null;

  // Worst-first: down → degraded → ok. Within the same status, the
  // oldest bar bubbles to the top so an operator scanning the table
  // sees the most-stale pair before anything else.
  const sorted = [...pairs].sort((a, b) => {
    const r = PAIR_STATUS_RANK[a.status] - PAIR_STATUS_RANK[b.status];
    if (r !== 0) return r;
    const ageA = a.age_seconds ?? Number.POSITIVE_INFINITY;
    const ageB = b.age_seconds ?? Number.POSITIVE_INFINITY;
    return ageB - ageA;
  });

  const fresh =
    typeof extras.fresh_count === "number" ? extras.fresh_count : null;
  const total =
    typeof extras.total_count === "number" ? extras.total_count : pairs.length;

  return (
    <section className="space-y-3" data-testid="crypto-freshness-widget">
      <h2 className="text-sm uppercase tracking-widest text-muted-foreground flex items-center gap-2 flex-wrap">
        <span>Crypto feed freshness</span>
        <span className="flex items-center gap-1 text-[10px]">
          <span className={`inline-block w-2 h-2 ${STATUS_DOT[card.status]}`} />
          {card.status}
        </span>
        <span className="text-[10px]">
          ({fresh ?? pairs.filter((p) => p.status === "ok").length}/{total} fresh)
        </span>
        {card.checked_at && (
          <span className="text-[10px] text-muted-foreground/70 normal-case tracking-normal">
            checked {card.checked_at}
          </span>
        )}
      </h2>
      <div className="border border-border bg-card overflow-x-auto text-xs">
        <table className="w-full">
          <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
            <tr className="border-b border-border">
              <th className="text-left p-2">status</th>
              <th className="text-left p-2">symbol</th>
              <th className="text-left p-2">granularity</th>
              <th className="text-left p-2">age</th>
              <th className="text-left p-2">threshold (yellow / red)</th>
              <th className="text-left p-2">last bar</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => (
              <tr
                key={`${p.symbol}-${p.granularity}`}
                className="border-b border-border/40"
                data-testid={`crypto-pair-${p.symbol}-${p.granularity}`}
                data-status={p.status}
              >
                <td className="p-2 whitespace-nowrap">
                  <span
                    className={`inline-flex items-center gap-1 uppercase tracking-widest text-[10px] ${PAIR_STATUS_TEXT[p.status]}`}
                  >
                    <span
                      className={`inline-block w-2 h-2 ${STATUS_DOT[p.status]}`}
                    />
                    {p.status}
                  </span>
                </td>
                <td className="p-2 whitespace-nowrap font-bold">{p.symbol}</td>
                <td className="p-2 whitespace-nowrap text-muted-foreground">
                  {p.granularity}
                </td>
                <td
                  className={`p-2 whitespace-nowrap ${PAIR_STATUS_TEXT[p.status]}`}
                  data-testid={`crypto-pair-age-${p.symbol}-${p.granularity}`}
                >
                  {p.age_seconds != null
                    ? formatPairAge(p.age_seconds)
                    : "no bars"}
                </td>
                <td className="p-2 whitespace-nowrap text-muted-foreground">
                  {p.yellow_threshold_seconds}s / {p.red_threshold_seconds}s
                </td>
                <td className="p-2 whitespace-nowrap text-muted-foreground normal-case tracking-normal break-all">
                  {p.max_ts ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function CardTile({ card }: { card: Card }) {
  const isFeed = card.key.startsWith("feed:");
  const isModels = card.key === "models";
  const isKalshiCluster = card.key === "kalshi_cluster";
  const extras =
    card.extras && typeof card.extras === "object"
      ? (card.extras as Record<string, unknown>)
      : {};
  const lastPull = isFeed
    ? ((extras.last_pull as string | null) ?? null)
    : null;
  const errorRate = isFeed
    ? ((extras.error_rate as number | null) ?? null)
    : null;
  const polls = isFeed
    ? ((extras.poll_count as number | null) ?? null)
    : null;
  const fails = isFeed
    ? ((extras.failure_count as number | null) ?? null)
    : null;
  // Soccer pollers stash a `metadata` dict on the feed status with the
  // currently-cached Sportradar season id and any 404-driven refresh
  // outcome (rolled over vs. stuck).  Surface those as a dedicated
  // line on the feed card so an operator can see at a glance which
  // season a poller is pinned to and whether the rollover handler
  // managed to recover.
  const metadata =
    isFeed && extras.metadata && typeof extras.metadata === "object"
      ? (extras.metadata as Record<string, unknown>)
      : null;
  const seasonId =
    metadata && typeof metadata.season_id === "string"
      ? metadata.season_id
      : null;
  const seasonResolvedAt =
    metadata && typeof metadata.season_id_resolved_at === "string"
      ? metadata.season_id_resolved_at
      : null;
  const seasonLastRefreshAt =
    metadata && typeof metadata.season_last_refresh_at === "string"
      ? metadata.season_last_refresh_at
      : null;
  const seasonLastRefreshOutcome =
    metadata && typeof metadata.season_last_refresh_outcome === "string"
      ? metadata.season_last_refresh_outcome
      : null;
  const seasonStuck = !!(metadata && metadata.season_stuck === true);
  const seasonPreviousId =
    metadata && typeof metadata.season_previous_id === "string"
      ? metadata.season_previous_id
      : null;
  // Bounded ring buffer of recent rotation/stuck attempts (task #648)
  // — surfaced as a collapsible block so an operator can scroll back
  // through past rotations without leaving the health card.  Each
  // entry has the shape `{at, from_id, to_id, outcome}`.  We narrow
  // the unknown payload defensively so a malformed entry can't crash
  // the React render.
  type SeasonHistoryEntry = {
    at: string | null;
    from_id: string | null;
    to_id: string | null;
    outcome: string;
  };
  const seasonHistory: SeasonHistoryEntry[] =
    metadata && Array.isArray(metadata.season_history)
      ? (metadata.season_history as unknown[]).flatMap((raw) => {
          if (!raw || typeof raw !== "object") return [];
          const e = raw as Record<string, unknown>;
          const outcome = typeof e.outcome === "string" ? e.outcome : null;
          if (!outcome) return [];
          return [{
            at: typeof e.at === "string" ? e.at : null,
            from_id: typeof e.from_id === "string" ? e.from_id : null,
            to_id: typeof e.to_id === "string" ? e.to_id : null,
            outcome,
          }];
        })
      : [];
  const cadenceSeconds =
    isFeed && typeof extras.cadence_seconds === "number"
      ? (extras.cadence_seconds as number)
      : null;
  // "At least one polling interval" — show the rolled-over badge for a
  // window equal to the feed's cadence (with a 60s floor so very fast
  // pollers still let humans notice it on a manual refresh).
  const refreshBadgeWindowMs = Math.max((cadenceSeconds ?? 60) * 1000, 60_000);
  const refreshAgeMs = seasonLastRefreshAt
    ? Date.now() - Date.parse(seasonLastRefreshAt)
    : Number.POSITIVE_INFINITY;
  const showRolledOverBadge =
    !seasonStuck &&
    seasonLastRefreshOutcome === "rotated" &&
    Number.isFinite(refreshAgeMs) &&
    refreshAgeMs >= 0 &&
    refreshAgeMs <= refreshBadgeWindowMs;
  // Sportradar tournament-leaderboard refresher (task #624) stashes a
  // per-cycle entrant count + a "live events" counter on the feed
  // metadata so the operator console can render a dedicated row next
  // to the other Sportradar entries instead of leaving operators to
  // parse the generic detail string.  The badge fires when the
  // refresher has been writing 0 rows for >10min while events are
  // live so a silent zero-row streak doesn't masquerade as a green
  // poll.
  const isTournamentLeaderboards =
    isFeed && card.key === "feed:sportradar_tournament_leaderboards";
  const leaderboardEventsLive =
    isTournamentLeaderboards &&
    metadata &&
    typeof metadata.events_live === "number"
      ? (metadata.events_live as number)
      : null;
  const leaderboardLastRefreshCount =
    isTournamentLeaderboards &&
    metadata &&
    typeof metadata.last_refresh_count === "number"
      ? (metadata.last_refresh_count as number)
      : null;
  const leaderboardLastRefreshAt =
    isTournamentLeaderboards &&
    metadata &&
    typeof metadata.last_refresh_at === "string"
      ? (metadata.last_refresh_at as string)
      : null;
  const leaderboardZeroRowsSince =
    isTournamentLeaderboards &&
    metadata &&
    typeof metadata.zero_rows_since === "string"
      ? (metadata.zero_rows_since as string)
      : null;
  // Per-event breakdown: the refresher stashes one row per tournament
  // it touched this cycle (sport + event id + entrants, plus an
  // optional error string) so operators can tell whether a single
  // flaky tournament is dragging the cycle total down vs. a
  // system-wide issue.
  const leaderboardPerEvent: Array<{
    sport: string;
    event_id: string;
    entrants: number;
    error?: string;
  }> =
    isTournamentLeaderboards && metadata && Array.isArray(metadata.per_event)
      ? (metadata.per_event as unknown[]).flatMap((entry) => {
          if (!entry || typeof entry !== "object") return [];
          const row = entry as Record<string, unknown>;
          const sport = typeof row.sport === "string" ? row.sport : null;
          const eventId =
            typeof row.event_id === "string"
              ? row.event_id
              : typeof row.event_id === "number"
                ? String(row.event_id)
                : null;
          const entrants =
            typeof row.entrants === "number" ? row.entrants : null;
          if (!sport || !eventId || entrants == null) return [];
          const error = typeof row.error === "string" ? row.error : undefined;
          return [{ sport, event_id: eventId, entrants, error }];
        })
      : [];
  const leaderboardLastSuccessAt = isTournamentLeaderboards
    ? ((extras.last_ok_at as string | null) ?? null)
    : null;
  const leaderboardZeroRowsAgeMs = leaderboardZeroRowsSince
    ? Date.now() - Date.parse(leaderboardZeroRowsSince)
    : Number.NaN;
  const showLeaderboardStaleBadge =
    isTournamentLeaderboards &&
    !!leaderboardZeroRowsSince &&
    (leaderboardEventsLive ?? 0) > 0 &&
    Number.isFinite(leaderboardZeroRowsAgeMs) &&
    leaderboardZeroRowsAgeMs > 10 * 60 * 1000;
  const access = useGetMyAccess();
  const { getToken } = useAuth();
  const isAdmin = !!access.data?.is_admin;
  const feedName = isFeed ? card.key.slice("feed:".length) : null;
  const [pollBusy, setPollBusy] = useState(false);
  const [pollResult, setPollResult] = useState<string | null>(null);
  async function pollNow() {
    if (!feedName) return;
    setPollBusy(true);
    setPollResult(null);
    try {
      const res = await apiFetch<{ name: string; written: number }>(
        `/api/admin/feeds/${encodeURIComponent(feedName)}/poll-now`,
        { method: "POST", body: {}, getToken },
      );
      setPollResult(`wrote ${res.written}`);
    } catch (e: unknown) {
      setPollResult(
        formatErrorMessage(
          e,
          e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
        ),
      );
    } finally {
      setPollBusy(false);
    }
  }
  return (
    <div className={`border ${STATUS_COLOR[card.status]} p-3 text-xs space-y-1`}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-bold uppercase tracking-widest">{card.label}</span>
        <span className="flex items-center gap-1 text-[10px] uppercase tracking-widest">
          <span className={`inline-block w-2 h-2 ${STATUS_DOT[card.status]}`} />
          {card.status}
        </span>
      </div>
      {isModels && (
        <div className="flex flex-wrap gap-1">
          <ManifestSignatureBadge extras={extras} />
        </div>
      )}
      {isKalshiCluster && <KalshiClusterDetails extras={extras} />}
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
      {isTournamentLeaderboards && (
        <div
          className="space-y-0.5 text-[10px] uppercase tracking-widest text-muted-foreground"
          data-testid={`feed-leaderboard-${card.key}`}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span>
              events live:{" "}
              <span
                className="text-foreground normal-case tracking-normal"
                data-testid={`feed-leaderboard-events-${card.key}`}
              >
                {leaderboardEventsLive ?? 0}
              </span>
            </span>
            <span>
              last refresh:{" "}
              <span
                className="text-foreground normal-case tracking-normal"
                data-testid={`feed-leaderboard-refresh-count-${card.key}`}
              >
                {leaderboardLastRefreshCount != null
                  ? `${leaderboardLastRefreshCount} entrants`
                  : "—"}
              </span>
            </span>
            {showLeaderboardStaleBadge && (
              <span
                className="border border-destructive/60 bg-destructive/10 text-destructive px-1 py-0.5"
                data-testid={`feed-leaderboard-stale-${card.key}`}
                title={
                  leaderboardZeroRowsSince
                    ? `Refresh has produced 0 rows since ${leaderboardZeroRowsSince}`
                    : "Refresh has produced 0 rows for >10 minutes"
                }
              >
                stale leaderboard
              </span>
            )}
          </div>
          <div>
            last success:{" "}
            <span
              className="text-foreground normal-case tracking-normal"
              data-testid={`feed-leaderboard-last-success-${card.key}`}
            >
              {leaderboardLastSuccessAt ?? "never"}
            </span>
            {leaderboardLastRefreshAt &&
              leaderboardLastRefreshAt !== leaderboardLastSuccessAt && (
                <>
                  {" "}
                  · attempted{" "}
                  <span className="text-foreground normal-case tracking-normal">
                    {leaderboardLastRefreshAt}
                  </span>
                </>
              )}
          </div>
          {leaderboardPerEvent.length > 0 && (
            <ul
              className="mt-1 space-y-0.5 normal-case tracking-normal"
              data-testid={`feed-leaderboard-per-event-${card.key}`}
            >
              {leaderboardPerEvent.map((row) => {
                const isZero = row.entrants === 0;
                return (
                  <li
                    key={`${row.sport}:${row.event_id}`}
                    className={`flex items-center justify-between gap-2 border px-1 py-0.5 ${
                      isZero
                        ? "border-destructive/60 bg-destructive/10 text-destructive"
                        : "border-border/40 text-foreground"
                    }`}
                    data-testid={`feed-leaderboard-event-${card.key}-${row.sport}-${row.event_id}`}
                    data-zero={isZero ? "true" : "false"}
                    title={row.error ?? undefined}
                  >
                    <span className="flex items-center gap-1 truncate">
                      <span className="uppercase tracking-widest text-[10px]">
                        {row.sport}
                      </span>
                      <span className="break-all">{row.event_id}</span>
                    </span>
                    <span className="shrink-0 tabular-nums">
                      {row.entrants} entrants
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
      {isFeed && metadata && (seasonId || seasonStuck) && (
        <div
          className="space-y-0.5 text-[10px] uppercase tracking-widest text-muted-foreground"
          data-testid={`feed-season-${card.key}`}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span>
              season:{" "}
              <span
                className="text-foreground normal-case tracking-normal break-all"
                data-testid={`feed-season-id-${card.key}`}
              >
                {seasonId ?? "n/a"}
              </span>
            </span>
            {seasonResolvedAt && (
              <span>
                resolved:{" "}
                <span className="text-foreground normal-case tracking-normal">
                  {seasonResolvedAt}
                </span>
              </span>
            )}
            {showRolledOverBadge && (
              <span
                className="border border-warning/60 bg-warning/10 text-warning px-1 py-0.5"
                data-testid={`feed-season-rolled-over-${card.key}`}
                title={
                  seasonPreviousId
                    ? `Refreshed from ${seasonPreviousId} at ${seasonLastRefreshAt}`
                    : `Refreshed at ${seasonLastRefreshAt}`
                }
              >
                season rolled over
              </span>
            )}
            {seasonStuck && (
              <span
                className="border border-destructive/60 bg-destructive/10 text-destructive px-1 py-0.5"
                data-testid={`feed-season-stuck-${card.key}`}
                title={
                  seasonLastRefreshOutcome
                    ? `Refresh outcome: ${seasonLastRefreshOutcome}`
                    : "Season id refresh failed"
                }
              >
                season id stuck
              </span>
            )}
          </div>
          {seasonLastRefreshAt && (
            <div>
              last refresh:{" "}
              <span className="text-foreground normal-case tracking-normal">
                {seasonLastRefreshAt}
                {seasonLastRefreshOutcome
                  ? ` (${seasonLastRefreshOutcome})`
                  : ""}
              </span>
            </div>
          )}
          {seasonHistory.length > 0 && (
            <details
              className="pt-1"
              data-testid={`feed-season-history-${card.key}`}
            >
              <summary
                className="cursor-pointer select-none text-muted-foreground/80 hover:text-foreground"
                data-testid={`feed-season-history-toggle-${card.key}`}
              >
                history ({seasonHistory.length})
              </summary>
              <ol
                className="mt-1 space-y-0.5 normal-case tracking-normal text-foreground"
                data-testid={`feed-season-history-list-${card.key}`}
              >
                {/* Newest first so an operator scanning the panel
                    sees the most recent rotation/stuck attempt at
                    the top instead of having to scroll past months
                    of history.  We slice to a copy before reversing
                    so we don't mutate the snapshot held in state. */}
                {[...seasonHistory].reverse().map((entry, idx) => (
                  <li
                    key={`${entry.at ?? "?"}-${idx}`}
                    className="flex flex-wrap gap-x-2 gap-y-0 text-[10px]"
                    data-testid={`feed-season-history-entry-${card.key}-${idx}`}
                    data-outcome={entry.outcome}
                  >
                    <span className="text-muted-foreground">
                      {entry.at ?? "—"}
                    </span>
                    <span className="break-all">
                      {entry.from_id ?? "—"} → {entry.to_id ?? "—"}
                    </span>
                    <span
                      className={
                        entry.outcome === "rotated"
                          ? "text-success uppercase tracking-widest"
                          : entry.outcome === "rotated_but_404"
                            ? "text-warning uppercase tracking-widest"
                            : "text-destructive uppercase tracking-widest"
                      }
                    >
                      {entry.outcome}
                    </span>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
      )}
      {isFeed && isAdmin && feedName && (
        <div className="flex items-center gap-2 pt-1">
          <button
            type="button"
            onClick={pollNow}
            disabled={pollBusy}
            className="border border-foreground/40 px-2 py-0.5 text-[10px] uppercase tracking-widest hover:bg-foreground hover:text-background disabled:opacity-50"
            data-testid={`feed-poll-now-${card.key}`}
          >
            {pollBusy ? "polling…" : "poll now"}
          </button>
          {pollResult && (
            <span className="text-[10px] normal-case tracking-normal text-muted-foreground">
              {pollResult}
            </span>
          )}
        </div>
      )}
      {card.key.startsWith("stale_cancel_baseline:") && (
        <StaleCancelBaselineBlock extras={extras} />
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

/**
 * Per-vertical stale-cancel baseline detail block (task #711).
 *
 * Renders a compact 24h sparkline of `cancelled` per cycle plus the
 * mean / σ / max badges. Operators read this card to verify the
 * volume-anomaly alert that just paged them is real (peak well above
 * the trailing baseline) or noise (single tall bar, flat history).
 */
function StaleCancelBaselineBlock({
  extras,
}: {
  extras: Record<string, unknown>;
}) {
  const recent = Array.isArray(extras.recent_cancelled)
    ? (extras.recent_cancelled as unknown[]).map((v) =>
        typeof v === "number" && Number.isFinite(v) ? v : 0,
      )
    : [];
  const mean = typeof extras.mean === "number" ? (extras.mean as number) : 0;
  const stdev = typeof extras.stdev === "number" ? (extras.stdev as number) : 0;
  const max = typeof extras.max === "number" ? (extras.max as number) : 0;
  const failedCycles =
    typeof extras.recent_failed_cycles === "number"
      ? (extras.recent_failed_cycles as number)
      : 0;
  const failedTotal =
    typeof extras.recent_failures_total === "number"
      ? (extras.recent_failures_total as number)
      : 0;
  const peak = Math.max(max, ...recent, 1);
  return (
    <div
      className="space-y-1"
      data-testid={`stale-cancel-baseline-${String(extras.vertical ?? "")}`}
    >
      {recent.length > 0 ? (
        <div
          className="flex items-end gap-px h-8 border border-border/40 bg-card p-0.5"
          data-testid={`stale-cancel-sparkline-${String(extras.vertical ?? "")}`}
        >
          {recent.map((v, i) => {
            const h = Math.max(1, Math.round((v / peak) * 28));
            const tone =
              v >= mean + 3 * stdev && stdev > 0
                ? "bg-destructive"
                : v >= mean + 2 * stdev && stdev > 0
                  ? "bg-warning"
                  : "bg-foreground/60";
            return (
              <div
                key={i}
                style={{ height: `${h}px` }}
                className={`w-1 ${tone}`}
                title={`cycle -${recent.length - i}: cancelled=${v}`}
              />
            );
          })}
        </div>
      ) : (
        <div className="text-[10px] text-muted-foreground italic">
          (no cycles in window — sparkline empty)
        </div>
      )}
      <div className="flex items-center gap-2 flex-wrap text-[10px] uppercase tracking-widest text-muted-foreground">
        <span>μ {mean.toFixed(1)}</span>
        <span>σ {stdev.toFixed(1)}</span>
        <span>peak {max}</span>
        {failedTotal > 0 && (
          <span className="text-warning">
            ⚠ failed_cycles={failedCycles} failed_total={failedTotal}
          </span>
        )}
      </div>
    </div>
  );
}

type CryptoAccuracyWindow = {
  window_hours: number;
  n_settled: number;
  n_scored: number;
  n_void: number;
  brier: number | null;
  hit_rate: number | null;
};

type CryptoAccuracyResponse = {
  league: string;
  windows: CryptoAccuracyWindow[];
};

function fmtBrier(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(4);
}

function fmtHitRate(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function windowLabel(hours: number): string {
  if (hours <= 0) return `${hours}h`;
  if (hours % 24 === 0) {
    const days = hours / 24;
    return days === 1 ? "24h" : `${days}d`;
  }
  return `${hours}h`;
}

export function CryptoAccuracyPanel({
  token,
}: {
  token: () => Promise<string | null>;
}) {
  const [data, setData] = useState<CryptoAccuracyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await apiFetch<CryptoAccuracyResponse>(
          "/api/dashboard/crypto-accuracy",
          { getToken: token },
        );
        if (cancelled) return;
        setData(res);
        setError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(
          formatErrorMessage(
            e,
            e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
          ),
        );
      }
    }
    load();
    // 60s cadence: realised accuracy moves only as new markets settle,
    // so the higher-frequency 15s candidates panel is enough live data.
    const id = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [token]);

  const windows = data?.windows ?? [];

  return (
    <section
      className="space-y-3"
      data-testid="panel-crypto-accuracy"
    >
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
          Crypto realised accuracy
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground/70">
          source :: predictions.kalshi_price vs settle_price · auto-refreshes every 60s
        </span>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Brier score and hit rate for settled Kalshi crypto-binary
        markets, scoring the market mid the producer recorded
        (<code>kalshi_price</code>) against the realised
        <code> settle_price</code> the settler stamped. Voids
        refund every contract and are excluded from scoring but
        counted under <code>void</code> for coverage.
      </p>
      {error && (
        <div className="border border-destructive bg-destructive/10 text-destructive p-2 text-xs">
          {error}
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {windows.length === 0 && !error && (
          <div
            className="border border-border bg-card p-3 text-xs text-muted-foreground sm:col-span-2"
            data-testid="crypto-accuracy-empty"
          >
            no settled crypto predictions in the rolling windows
          </div>
        )}
        {windows.map((w) => {
          const label = windowLabel(w.window_hours);
          return (
            <div
              key={w.window_hours}
              className="border border-border bg-card p-3 space-y-2"
              data-testid={`crypto-accuracy-window-${w.window_hours}h`}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
                  last {label}
                </span>
                <span
                  className="text-[10px] uppercase tracking-widest text-muted-foreground/70"
                  data-testid={`crypto-accuracy-window-${w.window_hours}h-counts`}
                >
                  scored {w.n_scored} · settled {w.n_settled} · void {w.n_void}
                </span>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    brier
                  </div>
                  <div
                    className="text-lg font-bold tabular-nums"
                    data-testid={`crypto-accuracy-window-${w.window_hours}h-brier`}
                  >
                    {fmtBrier(w.brier)}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    hit rate
                  </div>
                  <div
                    className="text-lg font-bold tabular-nums"
                    data-testid={`crypto-accuracy-window-${w.window_hours}h-hit-rate`}
                  >
                    {fmtHitRate(w.hit_rate)}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

type KalshiCryptoCandidate = {
  ticker: string | null;
  symbol: string | null;
  title: string | null;
  mid_price: number | null;
  close_time: string | null;
  updated_at: string | null;
  settled_at: string | null;
  outcome: string | null;
  settle_price: number | null;
};

type KalshiCryptoSettledRollup = {
  today: number;
  yesterday: number;
};

type KalshiCryptoCandidatesResponse = {
  count: number;
  items: KalshiCryptoCandidate[];
  limit: number;
  settled?: KalshiCryptoSettledRollup | null;
  error?: string | null;
};

function fmtSettlePrice(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(0)}¢`;
}

export function CryptoOutcomeBadge({
  outcome,
}: {
  outcome: string | null;
}) {
  if (!outcome) {
    return (
      <span
        className="text-muted-foreground/70"
        data-testid="crypto-outcome-pending"
      >
        pending
      </span>
    );
  }
  const norm = outcome.toLowerCase();
  // YES / NO / VOID get distinct, scannable styling so an operator
  // glancing at the panel can spot the realised side without parsing
  // the price.  VOID is muted because no contracts paid out.
  const cls =
    norm === "yes"
      ? "border-success/40 bg-success/10 text-success"
      : norm === "no"
        ? "border-destructive/40 bg-destructive/10 text-destructive"
        : "border-border bg-muted/30 text-muted-foreground";
  return (
    <span
      className={`inline-block border px-1.5 py-px text-[10px] uppercase tracking-widest font-bold ${cls}`}
      data-testid="crypto-outcome-badge"
      data-outcome={norm}
    >
      {norm}
    </span>
  );
}

function fmtCryptoTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").slice(0, 19) + "Z";
}

function fmtMidPrice(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}¢`;
}

export function KalshiCryptoCandidatesPanel({
  token,
}: {
  token: () => Promise<string | null>;
}) {
  const [data, setData] = useState<KalshiCryptoCandidatesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await apiFetch<KalshiCryptoCandidatesResponse>(
          "/api/feeds/kalshi-crypto-candidates?limit=25",
          { getToken: token },
        );
        if (cancelled) return;
        setData(res);
        setError(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(
          formatErrorMessage(
            e,
            e instanceof ApiError ? `HTTP ${e.status} :: ${e.message}` : undefined,
          ),
        );
      }
    }
    load();
    const id = setInterval(load, 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [token]);

  return (
    <section
      className="space-y-3"
      data-testid="panel-kalshi-crypto-candidates"
    >
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <h2 className="text-sm uppercase tracking-widest text-muted-foreground">
          Kalshi crypto candidates
          <span
            className="ml-2 text-[10px]"
            data-testid="kalshi-crypto-candidates-count"
          >
            ({data?.count ?? 0})
          </span>
          <span
            className="ml-3 text-[10px] normal-case tracking-normal text-muted-foreground/80"
            data-testid="kalshi-crypto-settled-rollup"
            title="Crypto predictions stamped by the kalshi_crypto_settlements settler, bucketed on the UTC day boundary."
          >
            settled today:{" "}
            <span
              className="font-bold text-foreground tabular-nums"
              data-testid="kalshi-crypto-settled-today"
            >
              {data?.settled?.today ?? 0}
            </span>{" "}
            · yesterday:{" "}
            <span
              className="font-bold text-foreground tabular-nums"
              data-testid="kalshi-crypto-settled-yesterday"
            >
              {data?.settled?.yesterday ?? 0}
            </span>
          </span>
        </h2>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground/70">
          feed :: kalshi_crypto_markets · auto-refreshes every 15s
        </span>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Live BTC/ETH binary markets the discovery feed has upserted into the
        crypto-binary cycle queue. Confirms the poller is feeding real
        candidates with prices and close times before a cycle fires; settled
        rows show the realised YES/NO/void outcome the settler stamped.
      </p>
      {error && (
        <div className="border border-destructive bg-destructive/10 text-destructive p-2 text-xs">
          {error}
        </div>
      )}
      {data?.error && (
        <div className="border border-warning/40 bg-warning/5 text-warning p-2 text-xs">
          {data.error}
        </div>
      )}
      <div className="border border-border bg-card max-h-72 overflow-y-auto text-xs">
        {(data?.items ?? []).length === 0 ? (
          <div
            className="p-3 text-muted-foreground"
            data-testid="kalshi-crypto-candidates-empty"
          >
            no crypto candidates queued
          </div>
        ) : (
          <table className="w-full">
            <thead className="text-[10px] uppercase tracking-widest text-muted-foreground">
              <tr className="border-b border-border">
                <th className="text-left p-2">ticker</th>
                <th className="text-left p-2">symbol</th>
                <th className="text-right p-2">mid</th>
                <th className="text-left p-2">close</th>
                <th className="text-left p-2">updated</th>
                <th className="text-left p-2">outcome</th>
                <th className="text-right p-2">settle</th>
                <th className="text-left p-2">settled</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((c, i) => (
                <tr
                  key={`${c.ticker ?? "row"}-${i}`}
                  className="border-b border-border/40"
                  data-testid={`kalshi-crypto-candidate-${i}`}
                  data-settled={c.settled_at ? "true" : "false"}
                >
                  <td className="p-2 break-all font-mono">
                    {c.ticker ?? "—"}
                  </td>
                  <td className="p-2 whitespace-nowrap uppercase tracking-widest">
                    {c.symbol ?? "—"}
                  </td>
                  <td className="p-2 text-right whitespace-nowrap font-mono">
                    {fmtMidPrice(c.mid_price)}
                  </td>
                  <td className="p-2 whitespace-nowrap text-muted-foreground">
                    {fmtCryptoTime(c.close_time)}
                  </td>
                  <td className="p-2 whitespace-nowrap text-muted-foreground">
                    {fmtCryptoTime(c.updated_at)}
                  </td>
                  <td
                    className="p-2 whitespace-nowrap"
                    data-testid={`kalshi-crypto-candidate-${i}-outcome`}
                  >
                    <CryptoOutcomeBadge outcome={c.outcome} />
                  </td>
                  <td
                    className="p-2 text-right whitespace-nowrap font-mono text-muted-foreground"
                    data-testid={`kalshi-crypto-candidate-${i}-settle-price`}
                  >
                    {fmtSettlePrice(c.settle_price)}
                  </td>
                  <td
                    className="p-2 whitespace-nowrap text-muted-foreground"
                    data-testid={`kalshi-crypto-candidate-${i}-settled-at`}
                  >
                    {fmtCryptoTime(c.settled_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

