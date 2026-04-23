import { useMemo, useState } from "react";
import { Link, useLocation } from "wouter";
import { useAuth, useClerk, useUser } from "@clerk/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, formatErrorMessage } from "@/lib/api";
import {
  ApiError,
  UserSubscriptionTier,
  getGetMyAccessQueryKey,
  getGetMySubscriptionQueryKey,
  useCreateBillingPortalSession,
  useGetMyAccess,
  useGetMySubscription,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

const TIER_COPY: Record<string, string> = {
  [UserSubscriptionTier.free]: "Read-only dashboard at delay.",
  [UserSubscriptionTier.pro]: "Live signals + full swarm transcripts.",
  [UserSubscriptionTier.elite]: "Pro + institutional-grade alerts.",
  [UserSubscriptionTier.founder]: "Operator allow-list — billing bypassed.",
};

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
    });
  } catch {
    return iso;
  }
}

export default function ProfilePage() {
  const { user, isLoaded: userLoaded } = useUser();
  const { signOut } = useClerk();
  const { getToken, isSignedIn } = useAuth();
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const [portalError, setPortalError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);

  // Raw apiFetch (rather than a generated React hook) so /profile doesn't
  // depend on a regen for /api/me/push-token and /api/me/telegram. Mirrors
  // the pattern used in settings-notifications.tsx.
  const tokenGetter = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  type PushDevice = {
    platform: string | null;
    last_seen_at: string | null;
    created_at: string | null;
    token_suffix: string;
  };
  type PushTokenStatus = { count: number; devices: PushDevice[] };
  const pushQuery = useQuery<PushTokenStatus>({
    queryKey: ["profile", "push-token"],
    queryFn: () =>
      apiFetch<PushTokenStatus>("/api/me/push-token", { getToken: tokenGetter }),
    enabled: !!isSignedIn,
    staleTime: 60_000,
  });

  type TelegramStatus = {
    linked: boolean;
    telegram_user_id?: number | null;
    telegram_username?: string | null;
    bound_at?: string | null;
    last_seen_at?: string | null;
    pending_link_code?: string | null;
    pending_link_expires_at?: string | null;
    bot_username?: string | null;
    bot_running?: boolean;
  };
  const telegramQuery = useQuery<TelegramStatus>({
    queryKey: ["profile", "telegram"],
    queryFn: () =>
      apiFetch<TelegramStatus>("/api/me/telegram", { getToken: tokenGetter }),
    enabled: !!isSignedIn,
    staleTime: 60_000,
  });

  const subQuery = useGetMySubscription({
    query: { queryKey: getGetMySubscriptionQueryKey() },
  });
  const accessQuery = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });

  const sub = subQuery.data ?? null;
  const access = accessQuery.data ?? null;
  const isAdmin = !!access?.is_admin;

  const portalMutation = useCreateBillingPortalSession();
  const portalLoading = portalMutation.isPending;

  const openPortal = async () => {
    setPortalError(null);
    try {
      const res = await portalMutation.mutateAsync();
      if (res.url) {
        window.location.href = res.url;
      } else {
        setPortalError("Stripe did not return a portal URL.");
      }
    } catch (e) {
      setPortalError(
        formatErrorMessage(
          e,
          e instanceof ApiError ? `Portal failed (HTTP ${e.status})` : undefined,
        ),
      );
    }
  };

  const refreshSubscription = () => {
    void queryClient.invalidateQueries({
      queryKey: getGetMySubscriptionQueryKey(),
    });
  };

  const handleSignOut = async () => {
    setSigningOut(true);
    try {
      await signOut({ redirectUrl: import.meta.env.BASE_URL });
    } catch {
      setSigningOut(false);
    }
  };

  const displayName =
    user?.fullName ??
    user?.username ??
    user?.primaryEmailAddress?.emailAddress ??
    "Operator";
  const email =
    user?.primaryEmailAddress?.emailAddress ??
    sub?.email ??
    access?.email ??
    null;
  const initials = displayName
    .split(/\s+/)
    .map((s) => s.charAt(0).toUpperCase())
    .slice(0, 2)
    .join("");

  const tier = sub?.tier ?? null;
  const status = sub?.status ?? null;

  return (
    <div className="space-y-5 font-mono" data-testid="page-profile">
      <header className="flex flex-col gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
          ACCOUNT
        </div>
        <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
          Profile
        </h1>
        <p className="mt-1 text-xs text-muted-foreground">
          Account identity, subscription, and session.
        </p>
      </header>

      <GlassCard tone="solid" padding="md" data-testid="card-profile-identity">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
          <div
            className="flex h-16 w-16 shrink-0 items-center justify-center rounded-full border border-border bg-muted/20 text-lg font-bold uppercase tracking-widest text-accent-cyan"
            data-testid="text-profile-initials"
          >
            {userLoaded ? initials || "··" : "··"}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-base font-semibold text-foreground" data-testid="text-profile-name">
              {displayName}
            </div>
            <div className="truncate text-xs text-muted-foreground" data-testid="text-profile-email">
              {email ?? "—"}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-widest">
              {tier && (
                <span
                  className="border border-primary/60 bg-primary/5 px-2 py-0.5 text-primary"
                  data-testid="badge-profile-tier"
                >
                  {tier}
                </span>
              )}
              {status && (
                <span
                  className={`border px-2 py-0.5 ${
                    status === "active" || status === "trialing"
                      ? "border-success/60 text-success"
                      : status === "past_due"
                        ? "border-destructive/60 text-destructive"
                        : "border-border text-muted-foreground"
                  }`}
                  data-testid="badge-profile-status"
                >
                  {status}
                </span>
              )}
              {isAdmin && (
                <span
                  className="border border-loss/60 bg-loss/5 px-2 py-0.5 text-loss"
                  data-testid="badge-profile-admin"
                >
                  Admin
                </span>
              )}
            </div>
          </div>
        </div>
      </GlassCard>

      <GlassCard tone="solid" padding="md" data-testid="card-profile-subscription">
        <SectionHeader
          eyebrow="Subscription"
          title="Plan & billing"
          subtitle={tier ? TIER_COPY[tier] ?? "" : "Loading subscription…"}
          actions={
            <button
              type="button"
              onClick={refreshSubscription}
              className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
              data-testid="button-refresh-subscription"
            >
              Refresh
            </button>
          }
        />

        {subQuery.isLoading && !sub && (
          <div className="text-xs text-muted-foreground" data-testid="text-subscription-loading">
            Loading…
          </div>
        )}

        {sub && (
          <dl className="grid grid-cols-1 gap-3 text-xs sm:grid-cols-2">
            <div>
              <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Interval
              </dt>
              <dd className="text-foreground" data-testid="text-profile-interval">
                {sub.interval ?? "—"}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Renews / ends
              </dt>
              <dd className="text-foreground" data-testid="text-profile-period-end">
                {fmtDate(sub.current_period_end)}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Cancel at period end
              </dt>
              <dd className="text-foreground" data-testid="text-profile-cancel">
                {sub.cancel_at_period_end ? "yes" : "no"}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Stripe customer
              </dt>
              <dd className="truncate text-muted-foreground" data-testid="text-profile-stripe">
                {sub.stripe_customer_id ?? "—"}
              </dd>
            </div>
          </dl>
        )}

        {portalError && (
          <div
            className="mt-3 border border-destructive p-2 text-xs text-destructive"
            data-testid="text-portal-error"
          >
            {portalError}
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2 border-t border-border pt-4">
          {sub?.is_founder ? (
            <span className="text-xs text-muted-foreground" data-testid="text-founder-notice">
              Founder accounts bypass billing entirely.
            </span>
          ) : sub?.stripe_customer_id ? (
            <button
              type="button"
              onClick={openPortal}
              disabled={portalLoading}
              className="bg-primary px-4 py-2 text-xs font-bold uppercase tracking-widest text-primary-foreground hover:opacity-90 disabled:opacity-50"
              data-testid="button-manage-subscription"
            >
              {portalLoading ? "Opening Stripe…" : "Manage subscription"}
            </button>
          ) : (
            <Link
              href="/pricing"
              className="bg-primary px-4 py-2 text-xs font-bold uppercase tracking-widest text-primary-foreground hover:opacity-90"
              data-testid="link-choose-plan"
            >
              Choose a plan
            </Link>
          )}
          {sub && sub.tier !== UserSubscriptionTier.founder && (
            <Link
              href="/pricing"
              className="border border-border px-4 py-2 text-xs uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary"
              data-testid="link-change-plan"
            >
              Change plan
            </Link>
          )}
        </div>
      </GlassCard>

      <GlassCard tone="solid" padding="md" data-testid="card-profile-push-token">
        <SectionHeader
          eyebrow="Mobile"
          title="Push devices"
          subtitle="Devices registered via Axiom Mobile that can receive push alerts."
        />
        {pushQuery.isLoading && !pushQuery.data ? (
          <div className="text-xs text-muted-foreground" data-testid="text-push-loading">
            Loading…
          </div>
        ) : pushQuery.isError ? (
          <div className="text-xs text-destructive" data-testid="text-push-error">
            Failed to load push device status.
          </div>
        ) : (pushQuery.data?.count ?? 0) === 0 ? (
          <div className="space-y-2 text-xs">
            <div className="flex items-center gap-2">
              <span
                className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground"
                data-testid="badge-push-status"
              >
                Not connected
              </span>
              <span className="text-muted-foreground">No mobile devices registered.</span>
            </div>
            <p className="text-muted-foreground">
              Install the Axiom Mobile app and sign in to start receiving live trade
              and halt push alerts on this account.
            </p>
          </div>
        ) : (
          <div className="space-y-2 text-xs">
            <div className="flex items-center gap-2">
              <span
                className="border border-success/60 bg-success/5 px-2 py-0.5 text-[10px] uppercase tracking-widest text-success"
                data-testid="badge-push-status"
              >
                Connected
              </span>
              <span className="text-muted-foreground" data-testid="text-push-count">
                {pushQuery.data!.count} device{pushQuery.data!.count === 1 ? "" : "s"} registered
              </span>
            </div>
            <ul className="space-y-1">
              {pushQuery.data!.devices.map((d, i) => (
                <li
                  key={`${d.token_suffix}-${i}`}
                  className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-1 text-[11px] text-muted-foreground"
                  data-testid={`row-push-device-${i}`}
                >
                  <span className="text-foreground">
                    {(d.platform ?? "device").toString().toUpperCase()}
                  </span>
                  <span className="text-[10px] tracking-widest">····{d.token_suffix}</span>
                  <span className="ml-auto text-[10px]">
                    last seen {fmtDate(d.last_seen_at)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </GlassCard>

      <GlassCard tone="solid" padding="md" data-testid="card-profile-telegram">
        <SectionHeader
          eyebrow="Telegram"
          title="Concierge bot"
          subtitle="Two-way Telegram concierge — link from System → Settings to receive trade alerts and issue /estop."
        />
        {telegramQuery.isLoading && !telegramQuery.data ? (
          <div className="text-xs text-muted-foreground" data-testid="text-telegram-loading">
            Loading…
          </div>
        ) : telegramQuery.isError ? (
          <div className="text-xs text-destructive" data-testid="text-telegram-error">
            Failed to load Telegram link status.
          </div>
        ) : telegramQuery.data?.linked ? (
          <div className="space-y-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className="border border-success/60 bg-success/5 px-2 py-0.5 text-[10px] uppercase tracking-widest text-success"
                data-testid="badge-telegram-status"
              >
                Linked
              </span>
              {telegramQuery.data.telegram_username && (
                <span className="text-foreground" data-testid="text-telegram-username">
                  @{telegramQuery.data.telegram_username}
                </span>
              )}
              <span className="text-muted-foreground">
                bound {fmtDate(telegramQuery.data.bound_at)}
              </span>
            </div>
            {telegramQuery.data.bot_username && (
              <div className="text-[11px] text-muted-foreground">
                Bot: @{telegramQuery.data.bot_username}
                {telegramQuery.data.bot_running === false && (
                  <span className="ml-2 text-destructive">(offline)</span>
                )}
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-2 text-xs">
            <div className="flex items-center gap-2">
              <span
                className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground"
                data-testid="badge-telegram-status"
              >
                Not linked
              </span>
              {telegramQuery.data?.bot_username && (
                <span className="text-muted-foreground">
                  Bot: @{telegramQuery.data.bot_username}
                </span>
              )}
            </div>
            <p className="text-muted-foreground">
              Link your Telegram account from{" "}
              <Link
                href="/system?tab=settings"
                className="text-accent-cyan underline hover:opacity-80"
                data-testid="link-telegram-settings"
              >
                System → Settings
              </Link>{" "}
              to enable concierge alerts.
            </p>
          </div>
        )}
      </GlassCard>

      <GlassCard tone="solid" padding="md" data-testid="card-profile-notifications">
        <SectionHeader
          eyebrow="Notifications"
          title="Alert channels"
          subtitle="Email · push · SMS · Telegram preferences live in System → Settings."
        />
        <button
          type="button"
          onClick={() => setLocation("/system?tab=settings")}
          className="border border-border px-3 py-2 text-xs uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary"
          data-testid="button-open-notification-prefs"
        >
          Open notification settings →
        </button>
      </GlassCard>

      <GlassCard tone="solid" padding="md" data-testid="card-profile-session">
        <SectionHeader
          eyebrow="Session"
          title="Sign out"
          subtitle="Ends this browser session. Mobile devices stay signed in."
        />
        <button
          type="button"
          onClick={handleSignOut}
          disabled={signingOut}
          className="border border-destructive bg-destructive/5 px-4 py-2 text-xs font-bold uppercase tracking-widest text-destructive hover:bg-destructive/10 disabled:opacity-50"
          data-testid="button-profile-sign-out"
        >
          {signingOut ? "Signing out…" : "Sign out"}
        </button>
      </GlassCard>
    </div>
  );
}
