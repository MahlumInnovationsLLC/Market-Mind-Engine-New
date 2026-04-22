import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { useAuth } from "@clerk/react";
import { apiFetch, ApiError } from "@/lib/api";

interface PrefsView {
  prefs: {
    notify_live_trade: boolean;
    notify_halt: boolean;
    email_daily_summary: boolean;
    email_drift: boolean;
    email_halt: boolean;
    email_live_trade: boolean;
    email_weekly_roundup: boolean;
    sms_live_trade: boolean;
    telegram_live_trade: boolean;
    sms_phone_number: string | null;
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

export default function SettingsNotificationsPage() {
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
        setError(e instanceof Error ? e.message : String(e));
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
      const msg =
        e instanceof ApiError && typeof e.body === "object" && e.body !== null
          ? `Save failed (HTTP ${e.status})`
          : e instanceof Error
            ? e.message
            : String(e);
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
    await update({
      sms_phone_number_present: true,
      sms_phone_number: next || null,
    });
  };

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-8 font-mono selection:bg-primary/30">
      <header className="max-w-3xl mx-auto mb-8 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">
            AXIOM_PREDICT :: NOTIFICATIONS
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            Per-channel alert preferences
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <Link
            href="/app"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-console"
          >
            ← Console
          </Link>
          <Link
            href="/billing"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-billing"
          >
            Billing
          </Link>
        </div>
      </header>

      <main className="max-w-3xl mx-auto space-y-6">
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
                {PREMIUM_TOGGLES.map((row) => (
                  <ToggleItem
                    key={row.key}
                    row={row}
                    checked={Boolean(data.prefs[row.key])}
                    disabled={saving || !data.elite_unlocked}
                    onChange={(v) => toggle(row.key, v)}
                  />
                ))}
              </div>

              <div className="border-t border-border pt-4 space-y-2">
                <label
                  htmlFor="sms-phone"
                  className="block text-[10px] uppercase tracking-widest text-muted-foreground"
                >
                  SMS phone number (E.164, e.g. +15551234567)
                </label>
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
}: {
  row: ToggleRow;
  checked: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
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
      </div>
    </label>
  );
}
