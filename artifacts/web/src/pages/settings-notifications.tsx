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
  };
  tier: string;
  elite_unlocked: boolean;
  elite_only_toggles: string[];
}

type ToggleKey = keyof PrefsView["prefs"] & string;

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
      await apiFetch<unknown>(
        "/api/me/notification-prefs/sms/start-verification",
        { method: "POST", getToken: token, body: {} },
      );
      setVerifyMsg("New code sent. Check your phone.");
    } catch (e) {
      setError(formatErrorMessage(e));
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
                        <button
                          type="button"
                          onClick={resendCode}
                          disabled={verifyBusy || saving}
                          className="border border-border px-3 py-2 text-xs uppercase tracking-widest hover:border-primary/50 disabled:opacity-50"
                          data-testid="button-resend-code"
                        >
                          Resend
                        </button>
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
          </>
        )}
      </main>
    </div>
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
