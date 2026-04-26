import { useEffect, useMemo, useState } from "react";
import { Link, Redirect } from "wouter";
import { useAuth } from "@clerk/react";
import { apiFetch, ApiError, formatErrorMessage } from "@/lib/api";

export default function SettingsNotificationsPage() {
  // /settings/notifications now lives inside the unified /system page
  // (Settings tab). Preserve the URL for older mobile builds and email
  // links by redirecting to the new home.
  return <Redirect to="/system?tab=settings" />;
}

interface PrefsView {
  prefs: {
    notify_live_trade: boolean;
    notify_halt: boolean;
    email_daily_summary: boolean;
    email_drift: boolean;
    email_halt: boolean;
    email_live_trade: boolean;
    email_weekly_roundup: boolean;
    email_trade_confirmation: boolean;
    digest_partials: boolean;
    sms_live_trade: boolean;
    telegram_live_trade: boolean;
    sms_phone_number: string | null;
    sms_phone_verified: boolean;
    sms_phone_verification_expires_at: string | null;
    // Server-issued cooldown hint (task #430). When set, the next
    // Resend tap will be rejected with HTTP 429 until this ISO
    // timestamp passes — the UI uses it to keep the Resend button
    // disabled across page refreshes.
    sms_phone_verification_next_resend_allowed_at: string | null;
  };
  tier: string;
  elite_unlocked: boolean;
  elite_only_toggles: string[];
}

type ToggleKey = keyof PrefsView["prefs"] & string;

interface RateLimitDetail {
  error?: string;
  reason?: string;
  retry_after_seconds?: number;
  next_resend_allowed_at?: string;
}

/**
 * The rate-limited start-verification endpoint returns FastAPI's standard
 * ``{detail: {...}}`` envelope. Pull the inner shape out defensively
 * — anything off the happy path falls back to ``null`` so the caller
 * just shows a generic message.
 */
function extractRateLimitDetail(body: unknown): RateLimitDetail | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  const candidate = detail && typeof detail === "object" ? detail : body;
  if (!candidate || typeof candidate !== "object") return null;
  const out: RateLimitDetail = {};
  const c = candidate as Record<string, unknown>;
  if (typeof c.error === "string") out.error = c.error;
  if (typeof c.reason === "string") out.reason = c.reason;
  if (typeof c.retry_after_seconds === "number") {
    out.retry_after_seconds = c.retry_after_seconds;
  }
  if (typeof c.next_resend_allowed_at === "string") {
    out.next_resend_allowed_at = c.next_resend_allowed_at;
  }
  return out;
}

interface HistoryChannel {
  kind: "email" | "push" | "sms" | "telegram";
  pref_column: string | null;
  opted_in: boolean;
}

interface HistoryItem {
  alert_type: string;
  label: string;
  scope: "broadcast" | "user";
  sent_at: string | null;
  dedupe_key: string;
  channels: HistoryChannel[];
  attempted: boolean;
}

interface ToggleRow {
  key: ToggleKey;
  label: string;
  description: string;
  channel: "push" | "email" | "sms" | "telegram";
}

const EMAIL_TOGGLES: ToggleRow[] = [
  {
    key: "email_daily_summary",
    label: "Daily P&L summary",
    description: "Once-per-UTC-day P&L recap with vertical & persona breakdowns.",
    channel: "email",
  },
  {
    key: "email_weekly_roundup",
    label: "Weekly roundup",
    description: "Friday digest of realized P&L, win rate, and notable trades.",
    channel: "email",
  },
  {
    key: "email_drift",
    label: "Drift alerts",
    description: "Calibration / Brier-score threshold breaches per vertical.",
    channel: "email",
  },
  {
    key: "email_halt",
    label: "Trading halts",
    description: "Kill-switch trips and emergency stops.",
    channel: "email",
  },
  {
    key: "email_live_trade",
    label: "Live trade fills",
    description:
      "Every non-paper Kalshi fill — high volume; opt-in only.",
    channel: "email",
  },
  {
    key: "email_trade_confirmation",
    label: "Trade confirmations",
    description:
      "Branded HTML receipt for each of your fills, with market, side, qty, price, cost, and updated position.",
    channel: "email",
  },
  {
    key: "digest_partials",
    label: "Consolidate partial fills",
    description:
      "Send a single confirmation when the whole order finishes instead of one per partial fill.",
    channel: "email",
  },
];

const PUSH_TOGGLES: ToggleRow[] = [
  {
    key: "notify_live_trade",
    label: "Live trade fills",
    description: "Mobile push for every non-paper fill.",
    channel: "push",
  },
  {
    key: "notify_halt",
    label: "Trading halts",
    description: "Mobile push when the kill switch trips.",
    channel: "push",
  },
];

const PREMIUM_TOGGLES: ToggleRow[] = [
  {
    key: "sms_live_trade",
    label: "Live trades — SMS",
    description: "Text-message alert for every live fill.",
    channel: "sms",
  },
  {
    key: "telegram_live_trade",
    label: "Live trades — Telegram",
    description: "Push to your bound Telegram concierge chat.",
    channel: "telegram",
  },
];

export function NotificationPrefsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const [data, setData] = useState<PrefsView | null>(null);
  const [phone, setPhone] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [verifyCode, setVerifyCode] = useState<string>("");
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [verifyMsg, setVerifyMsg] = useState<string | null>(null);
  // Wall-clock ms after which the user may tap "Resend" again.
  // Driven by the server-issued cooldown hint (task #430) — either the
  // ``sms_phone_verification_next_resend_allowed_at`` field on the prefs
  // payload (restored on every page load) or the ``next_resend_allowed_at``
  // echoed back inside a 429 response body.
  const [resendCooldownUntil, setResendCooldownUntil] = useState<number | null>(
    null,
  );
  // Tick state used solely to force a re-render once a second so the
  // button label can show "Resend in N s" and re-enable when the
  // cooldown elapses.
  const [, setNowTick] = useState(0);
  const [history, setHistory] = useState<HistoryItem[] | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);

  useEffect(() => {
    if (!isSignedIn) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    apiFetch<PrefsView>("/api/me/notification-prefs", { getToken: token })
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setPhone(d.prefs.sms_phone_number ?? "");
        // Restore the Resend cooldown across hard refreshes — without
        // this a user who hits the cooldown then reloads sees an
        // enabled button that immediately 429s on tap.
        const hint = d.prefs.sms_phone_verification_next_resend_allowed_at;
        const ts = hint ? Date.parse(hint) : NaN;
        setResendCooldownUntil(
          Number.isFinite(ts) && ts > Date.now() ? ts : null,
        );
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(formatErrorMessage(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isSignedIn, token]);

  // Re-render once a second while a cooldown is active so the button
  // label updates and naturally re-enables when the timer hits zero.
  useEffect(() => {
    if (resendCooldownUntil === null) return;
    const timer = window.setInterval(() => {
      if (Date.now() >= resendCooldownUntil) {
        setResendCooldownUntil(null);
      } else {
        setNowTick((n) => n + 1);
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [resendCooldownUntil]);

  const loadHistory = useMemo(
    () => async () => {
      setHistoryLoading(true);
      setHistoryError(null);
      try {
        const res = await apiFetch<{ items: HistoryItem[] }>(
          "/api/me/notification-history?limit=30",
          { getToken: token },
        );
        setHistory(res.items ?? []);
      } catch (e) {
        setHistoryError(formatErrorMessage(e));
        setHistory([]);
      } finally {
        setHistoryLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    if (!isSignedIn) {
      setHistoryLoading(false);
      return;
    }
    loadHistory();
  }, [isSignedIn, loadHistory]);

  const update = async (patch: Record<string, unknown>) => {
    setSaving(true);
    setError(null);
    try {
      const next = await apiFetch<PrefsView>("/api/me/notification-prefs", {
        method: "PUT",
        getToken: token,
        body: patch,
      });
      setData(next);
      setPhone(next.prefs.sms_phone_number ?? "");
      // The PUT may have auto-fired a verification SMS (when the phone
      // changed) which advances the server-side cooldown — sync the UI
      // immediately so the Resend button doesn't appear tappable.
      const hint = next.prefs.sms_phone_verification_next_resend_allowed_at;
      const ts = hint ? Date.parse(hint) : NaN;
      setResendCooldownUntil(
        Number.isFinite(ts) && ts > Date.now() ? ts : null,
      );
      setSavedAt(Date.now());
    } catch (e) {
      const msg = formatErrorMessage(
        e,
        e instanceof ApiError && typeof e.body === "object" && e.body !== null
          ? `Save failed (HTTP ${e.status})`
          : undefined,
      );
      setError(msg);
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (key: ToggleKey, value: boolean) => {
    await update({ [key]: value });
  };

  const savePhone = async () => {
    const next = phone.trim();
    setVerifyMsg(null);
    setVerifyCode("");
    await update({
      sms_phone_number_present: true,
      sms_phone_number: next || null,
    });
    if (next) {
      setVerifyMsg(
        "We just texted a 6-digit verification code. Enter it below to confirm this number.",
      );
    }
  };

  const resendCode = async () => {
    setVerifyBusy(true);
    setVerifyMsg(null);
    setError(null);
    try {
      const res = await apiFetch<{
        next_resend_allowed_at?: string | null;
      }>(
        "/api/me/notification-prefs/sms/start-verification",
        { method: "POST", getToken: token, body: {} },
      );
      // Server tells us exactly when the next resend will be allowed —
      // arm the cooldown so the button disables itself before the user
      // can spam-tap and rack up SMS charges (task #430).
      const hint = res.next_resend_allowed_at;
      const ts = hint ? Date.parse(hint) : NaN;
      if (Number.isFinite(ts) && ts > Date.now()) {
        setResendCooldownUntil(ts);
      }
      setVerifyMsg("New code sent. Check your phone.");
    } catch (e) {
      // Surface the rate-limit hint on a 429 so the button stays
      // disabled even though the request technically failed.
      if (e instanceof ApiError && e.status === 429) {
        const detail = extractRateLimitDetail(e.body);
        if (detail?.next_resend_allowed_at) {
          const ts = Date.parse(detail.next_resend_allowed_at);
          if (Number.isFinite(ts) && ts > Date.now()) {
            setResendCooldownUntil(ts);
          }
        }
        const wait = detail?.retry_after_seconds ?? null;
        setError(
          wait
            ? `Too many resend requests — please wait ${wait}s before trying again.`
            : "Too many resend requests. Please wait a moment.",
        );
      } else {
        setError(formatErrorMessage(e));
      }
    } finally {
      setVerifyBusy(false);
    }
  };

  const submitCode = async () => {
    const code = verifyCode.trim();
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the 6-digit code we texted you.");
      return;
    }
    setVerifyBusy(true);
    setVerifyMsg(null);
    setError(null);
    try {
      const next = await apiFetch<PrefsView>(
        "/api/me/notification-prefs/sms/verify",
        { method: "POST", getToken: token, body: { code } },
      );
      setData(next);
      setPhone(next.prefs.sms_phone_number ?? "");
      setVerifyCode("");
      // Number is verified; the Resend button is gone from the DOM,
      // but clear the timer anyway so a future re-edit doesn't inherit
      // a stale cooldown.
      setResendCooldownUntil(null);
      setVerifyMsg("Phone number verified. SMS alerts are unlocked.");
    } catch (e) {
      setError(formatErrorMessage(e, "Code was rejected. Try again."));
    } finally {
      setVerifyBusy(false);
    }
  };

  return (
    <div className="font-mono" data-testid="panel-notification-prefs">
      <main className="max-w-3xl space-y-6">
        {!isSignedIn && (
          <div
            className="border border-border bg-card p-6 text-center text-sm text-muted-foreground"
            data-testid="text-signin-required"
          >
            Please sign in to manage your notification preferences.
          </div>
        )}

        {error && (
          <div
            className="border border-destructive text-destructive p-3 text-xs uppercase tracking-widest"
            data-testid="text-error"
          >
            {error}
          </div>
        )}

        {loading && isSignedIn && (
          <div
            className="border border-border bg-card p-6 text-center text-xs uppercase tracking-widest text-muted-foreground"
            data-testid="text-loading"
          >
            Loading preferences…
          </div>
        )}

        {data && (
          <>
            <section
              className="border border-border bg-card p-6 space-y-4"
              data-testid="section-tier"
            >
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    Current tier
                  </div>
                  <div
                    className="text-2xl text-primary font-bold uppercase tracking-tight"
                    data-testid="text-tier"
                  >
                    {data.tier}
                  </div>
                </div>
                {!data.elite_unlocked && (
                  <Link
                    href="/pricing"
                    className="bg-primary text-primary-foreground px-3 py-2 text-xs uppercase tracking-widest font-bold hover:opacity-90"
                    data-testid="link-upgrade"
                  >
                    Upgrade to Elite
                  </Link>
                )}
              </div>
              {savedAt && (
                <div
                  className="text-[10px] uppercase tracking-widest text-primary"
                  data-testid="text-saved"
                >
                  ✓ Saved
                </div>
              )}
            </section>

            <PrefsGroup
              title="Email"
              caption="Sent from alerts@axiom-predict.app"
              rows={EMAIL_TOGGLES}
              data={data}
              saving={saving}
              onToggle={toggle}
            />

            <PrefsGroup
              title="Mobile push"
              caption="Sent to devices linked through the Axiom Mobile app."
              rows={PUSH_TOGGLES}
              data={data}
              saving={saving}
              onToggle={toggle}
            />

            <section
              className="border border-border bg-card p-6 space-y-4"
              data-testid="section-premium"
            >
              <div className="flex items-center justify-between">
                <h2 className="text-sm uppercase tracking-widest text-foreground font-bold">
                  Elite premium channels
                </h2>
                <span
                  className={`text-[10px] uppercase tracking-widest border px-2 py-0.5 ${
                    data.elite_unlocked
                      ? "border-primary text-primary"
                      : "border-border text-muted-foreground"
                  }`}
                  data-testid="text-elite-status"
                >
                  {data.elite_unlocked ? "Unlocked" : "Elite-only"}
                </span>
              </div>
              {!data.elite_unlocked && (
                <p
                  className="text-xs text-muted-foreground"
                  data-testid="text-elite-hint"
                >
                  SMS and Telegram alerts on live trade execution are
                  reserved for Elite subscribers.{" "}
                  <Link
                    href="/pricing"
                    className="underline text-primary hover:opacity-80"
                  >
                    Upgrade
                  </Link>{" "}
                  to enable them.
                </p>
              )}
              <div className="space-y-3">
                {PREMIUM_TOGGLES.map((row) => {
                  const needsVerification =
                    row.channel === "sms" && !data.prefs.sms_phone_verified;
                  return (
                    <ToggleItem
                      key={row.key}
                      row={row}
                      checked={Boolean(data.prefs[row.key])}
                      disabled={
                        saving || !data.elite_unlocked || needsVerification
                      }
                      hint={
                        needsVerification
                          ? "Verify your phone number below to enable SMS alerts."
                          : undefined
                      }
                      onChange={(v) => toggle(row.key, v)}
                    />
                  );
                })}
              </div>

              <div className="border-t border-border pt-4 space-y-3">
                <div className="flex items-center justify-between gap-2">
                  <label
                    htmlFor="sms-phone"
                    className="block text-[10px] uppercase tracking-widest text-muted-foreground"
                  >
                    SMS phone number (E.164, e.g. +15551234567)
                  </label>
                  {data.prefs.sms_phone_number ? (
                    <span
                      className={`text-[10px] uppercase tracking-widest border px-2 py-0.5 ${
                        data.prefs.sms_phone_verified
                          ? "border-primary text-primary"
                          : "border-destructive text-destructive"
                      }`}
                      data-testid="text-phone-verified-status"
                    >
                      {data.prefs.sms_phone_verified ? "Verified" : "Unverified"}
                    </span>
                  ) : null}
                </div>
                <div className="flex gap-2">
                  <input
                    id="sms-phone"
                    type="tel"
                    value={phone}
                    placeholder="+15551234567"
                    onChange={(e) => setPhone(e.target.value)}
                    disabled={saving || !data.elite_unlocked}
                    className="flex-1 bg-input border border-border text-foreground rounded-md px-3 py-2 text-xs disabled:opacity-50"
                    data-testid="input-sms-phone"
                  />
                  <button
                    type="button"
                    onClick={savePhone}
                    disabled={saving || !data.elite_unlocked}
                    className="bg-primary text-primary-foreground px-4 py-2 text-xs uppercase tracking-widest font-bold hover:opacity-90 disabled:opacity-50"
                    data-testid="button-save-phone"
                  >
                    Save phone
                  </button>
                </div>

                {data.elite_unlocked &&
                  data.prefs.sms_phone_number &&
                  !data.prefs.sms_phone_verified && (
                    <div
                      className="space-y-2 border border-border rounded-md p-3 bg-muted/30"
                      data-testid="section-phone-verify"
                    >
                      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                        Confirm ownership
                      </div>
                      <p className="text-[11px] text-muted-foreground">
                        We sent a 6-digit code to{" "}
                        <span className="text-foreground">
                          {data.prefs.sms_phone_number}
                        </span>
                        . Enter it below to enable paid SMS alerts.
                      </p>
                      <div className="flex gap-2">
                        <input
                          type="text"
                          inputMode="numeric"
                          autoComplete="one-time-code"
                          maxLength={6}
                          value={verifyCode}
                          placeholder="123456"
                          onChange={(e) =>
                            setVerifyCode(
                              e.target.value.replace(/\D/g, "").slice(0, 6),
                            )
                          }
                          disabled={verifyBusy || saving}
                          className="flex-1 bg-input border border-border text-foreground rounded-md px-3 py-2 text-xs tracking-[0.4em] text-center disabled:opacity-50"
                          data-testid="input-sms-code"
                        />
                        <button
                          type="button"
                          onClick={submitCode}
                          disabled={verifyBusy || saving || verifyCode.length !== 6}
                          className="bg-primary text-primary-foreground px-4 py-2 text-xs uppercase tracking-widest font-bold hover:opacity-90 disabled:opacity-50"
                          data-testid="button-verify-code"
                        >
                          Verify
                        </button>
                        {(() => {
                          const remainingMs =
                            resendCooldownUntil !== null
                              ? resendCooldownUntil - Date.now()
                              : 0;
                          const cooling = remainingMs > 0;
                          const remainingSec = Math.ceil(remainingMs / 1000);
                          return (
                            <button
                              type="button"
                              onClick={resendCode}
                              disabled={verifyBusy || saving || cooling}
                              title={
                                cooling
                                  ? `Wait ${remainingSec}s before requesting another code.`
                                  : undefined
                              }
                              className="border border-border px-3 py-2 text-xs uppercase tracking-widest hover:border-primary/50 disabled:opacity-50"
                              data-testid="button-resend-code"
                            >
                              {cooling ? `Resend (${remainingSec}s)` : "Resend"}
                            </button>
                          );
                        })()}
                      </div>
                    </div>
                  )}

                {verifyMsg && (
                  <div
                    className="text-[11px] text-primary"
                    data-testid="text-verify-msg"
                  >
                    {verifyMsg}
                  </div>
                )}
              </div>
            </section>

            <RecentAlertsPanel
              items={history}
              loading={historyLoading}
              error={historyError}
              onReload={loadHistory}
            />
          </>
        )}
      </main>
    </div>
  );
}

function RecentAlertsPanel({
  items,
  loading,
  error,
  onReload,
}: {
  items: HistoryItem[] | null;
  loading: boolean;
  error: string | null;
  onReload: () => void | Promise<void>;
}) {
  return (
    <section
      className="border border-border bg-card p-6 space-y-4"
      data-testid="section-recent-alerts"
    >
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm uppercase tracking-widest text-foreground font-bold">
            Recent alerts
          </h2>
          <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Last 30 alerts you were eligible to receive.
          </p>
        </div>
        <button
          type="button"
          onClick={() => onReload()}
          disabled={loading}
          className="border border-border text-xs uppercase tracking-widest px-3 py-1 hover:border-primary/50 disabled:opacity-50"
          data-testid="button-reload-history"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && (
        <div
          className="border border-destructive text-destructive p-3 text-xs uppercase tracking-widest"
          data-testid="text-history-error"
        >
          {error}
        </div>
      )}

      {!error && loading && (
        <div
          className="text-xs uppercase tracking-widest text-muted-foreground"
          data-testid="text-history-loading"
        >
          Loading recent alerts…
        </div>
      )}

      {!error && !loading && items && items.length === 0 && (
        <div
          className="text-xs text-muted-foreground"
          data-testid="text-history-empty"
        >
          No recent alerts dispatched yet. Once you start receiving
          notifications, the last 30 will show up here.
        </div>
      )}

      {!error && !loading && items && items.length > 0 && (
        <ul className="divide-y divide-border" data-testid="list-history">
          {items.map((item, idx) => (
            <li
              key={`${item.dedupe_key}-${idx}`}
              className="py-3 flex items-start justify-between gap-3"
              data-testid={`history-row-${idx}`}
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm text-foreground font-medium">
                  {item.label}
                </div>
                <div className="text-[11px] text-muted-foreground">
                  {item.sent_at
                    ? new Date(item.sent_at).toLocaleString()
                    : "(no timestamp)"}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1 justify-end">
                {item.channels.map((c) => (
                  <span
                    key={c.kind}
                    className={`text-[10px] uppercase tracking-widest border px-2 py-0.5 ${
                      c.opted_in
                        ? "border-primary text-primary"
                        : "border-border text-muted-foreground line-through"
                    }`}
                    data-testid={`history-channel-${idx}-${c.kind}`}
                    title={
                      c.opted_in
                        ? `Sent via ${c.kind}`
                        : `${c.kind} channel — you weren't opted in`
                    }
                  >
                    {c.kind}
                  </span>
                ))}
                <span
                  className={`text-[10px] uppercase tracking-widest border px-2 py-0.5 ${
                    item.attempted
                      ? "border-primary text-primary"
                      : "border-destructive text-destructive"
                  }`}
                  data-testid={`history-status-${idx}`}
                  title="The dispatch was attempted. Per-channel delivery status isn't tracked yet."
                >
                  {item.attempted ? "Attempted" : "Skipped"}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PrefsGroup({
  title,
  caption,
  rows,
  data,
  saving,
  onToggle,
}: {
  title: string;
  caption: string;
  rows: ToggleRow[];
  data: PrefsView;
  saving: boolean;
  onToggle: (key: ToggleKey, value: boolean) => Promise<void>;
}) {
  return (
    <section
      className="border border-border bg-card p-6 space-y-4"
      data-testid={`section-${title.toLowerCase().replace(/\s+/g, "-")}`}
    >
      <div>
        <h2 className="text-sm uppercase tracking-widest text-foreground font-bold">
          {title}
        </h2>
        <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {caption}
        </p>
      </div>
      <div className="space-y-3">
        {rows.map((row) => (
          <ToggleItem
            key={row.key}
            row={row}
            checked={Boolean(data.prefs[row.key])}
            disabled={saving}
            onChange={(v) => onToggle(row.key, v)}
          />
        ))}
      </div>
    </section>
  );
}

function ToggleItem({
  row,
  checked,
  disabled,
  onChange,
  hint,
}: {
  row: ToggleRow;
  checked: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
  hint?: string;
}) {
  return (
    <label
      className={`flex items-start gap-3 border border-border rounded-md px-3 py-3 ${
        disabled ? "opacity-60" : "hover:border-primary/50"
      }`}
      data-testid={`toggle-${row.key}`}
    >
      <input
        type="checkbox"
        className="mt-1 h-4 w-4 accent-primary"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        data-testid={`checkbox-${row.key}`}
      />
      <div className="flex-1">
        <div className="text-sm text-foreground font-medium">{row.label}</div>
        <div className="text-[11px] text-muted-foreground">{row.description}</div>
        {hint && (
          <div
            className="text-[11px] text-destructive mt-1"
            data-testid={`hint-${row.key}`}
          >
            {hint}
          </div>
        )}
      </div>
    </label>
  );
}
