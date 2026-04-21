import { useEffect, useState } from "react";
import { Link, useLocation } from "wouter";
import { useAuth, useClerk } from "@clerk/react";
import { apiFetch, ApiError } from "@/lib/api";

interface UserSubscription {
  user_id: string;
  email: string | null;
  tier: "free" | "pro" | "elite" | "founder";
  status: string;
  is_founder: boolean;
  stripe_customer_id: string | null;
  stripe_subscription_id: string | null;
  stripe_price_id: string | null;
  interval: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
}

const TIER_COPY: Record<UserSubscription["tier"], string> = {
  free: "Free — read-only dashboard at delay.",
  pro: "Pro — live signals + full swarm transcripts.",
  elite: "Elite — Pro + institutional features.",
  founder: "Founder — operator allow-list, billing bypassed.",
};

function fmtDate(iso: string | null): string {
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

export default function BillingPage() {
  const { getToken, isSignedIn } = useAuth();
  const { signOut } = useClerk();
  const [location] = useLocation();
  const [sub, setSub] = useState<UserSubscription | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [portalLoading, setPortalLoading] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const showSuccess = location.includes("checkout=success");

  useEffect(() => {
    if (!isSignedIn) return;
    let cancel = false;
    setLoading(true);
    apiFetch<UserSubscription>("/api/me/subscription", {
      getToken: async () => (await getToken()) ?? null,
    })
      .then((data) => {
        if (!cancel) setSub(data);
      })
      .catch((e) => {
        if (!cancel) {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (!cancel) setLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [isSignedIn, getToken, refreshKey]);

  const openPortal = async () => {
    setPortalLoading(true);
    setError(null);
    try {
      const res = await apiFetch<{ url: string }>(
        "/api/billing/portal-session",
        {
          method: "POST",
          getToken: async () => (await getToken()) ?? null,
        },
      );
      if (res.url) {
        window.location.href = res.url;
      } else {
        setError("Stripe did not return a portal URL.");
      }
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `Portal failed (HTTP ${e.status})`
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setPortalLoading(false);
    }
  };

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-8 font-mono selection:bg-primary/30">
      <header className="max-w-3xl mx-auto mb-8 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">
            AXIOM_PREDICT :: BILLING
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            Manage your subscription
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <Link
            href="/"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-home"
          >
            ← Console
          </Link>
          <Link
            href="/pricing"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-pricing"
          >
            Pricing
          </Link>
          {isSignedIn && (
            <button
              type="button"
              onClick={() => signOut({ redirectUrl: import.meta.env.BASE_URL })}
              className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-destructive hover:border-destructive"
              data-testid="button-sign-out"
            >
              Sign out
            </button>
          )}
        </div>
      </header>

      <main className="max-w-3xl mx-auto space-y-6">
        {!isSignedIn && (
          <div
            className="border border-border bg-card p-6 text-center text-sm text-muted-foreground"
            data-testid="text-signin-required"
          >
            Please sign in to manage your subscription.
          </div>
        )}

        {showSuccess && (
          <div
            className="border border-primary text-primary p-3 text-xs uppercase tracking-widest"
            data-testid="text-checkout-success"
          >
            ✓ Checkout complete. Your subscription will be active in moments.
            <button
              type="button"
              onClick={() => setRefreshKey((k) => k + 1)}
              className="ml-3 underline"
              data-testid="button-refresh-after-checkout"
            >
              Refresh
            </button>
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

        {isSignedIn && loading && !sub && (
          <div
            className="border border-border bg-card p-6 text-center text-xs uppercase tracking-widest text-muted-foreground"
            data-testid="text-loading"
          >
            Loading subscription…
          </div>
        )}

        {sub && (
          <div className="border border-border bg-card p-6 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Current plan
                </div>
                <div
                  className="text-2xl text-primary font-bold uppercase tracking-tight"
                  data-testid="text-current-tier"
                >
                  {sub.tier}
                </div>
              </div>
              <div
                className={`text-[10px] uppercase tracking-widest px-2 py-1 border ${
                  sub.status === "active" || sub.status === "trialing"
                    ? "border-primary text-primary"
                    : sub.status === "past_due"
                      ? "border-destructive text-destructive"
                      : "border-border text-muted-foreground"
                }`}
                data-testid="text-current-status"
              >
                {sub.status}
              </div>
            </div>

            <p
              className="text-xs text-muted-foreground"
              data-testid="text-tier-copy"
            >
              {TIER_COPY[sub.tier]}
            </p>

            <dl className="grid grid-cols-2 gap-3 text-xs border-t border-border pt-4">
              <div>
                <dt className="text-muted-foreground uppercase tracking-widest text-[10px]">
                  Email
                </dt>
                <dd
                  className="text-foreground truncate"
                  data-testid="text-email"
                >
                  {sub.email ?? "—"}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground uppercase tracking-widest text-[10px]">
                  Interval
                </dt>
                <dd
                  className="text-foreground"
                  data-testid="text-interval"
                >
                  {sub.interval ?? "—"}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground uppercase tracking-widest text-[10px]">
                  Renews / ends
                </dt>
                <dd
                  className="text-foreground"
                  data-testid="text-period-end"
                >
                  {fmtDate(sub.current_period_end)}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground uppercase tracking-widest text-[10px]">
                  Cancel at period end
                </dt>
                <dd
                  className="text-foreground"
                  data-testid="text-cancel-at-period-end"
                >
                  {sub.cancel_at_period_end ? "yes" : "no"}
                </dd>
              </div>
            </dl>

            <div className="border-t border-border pt-4 flex flex-wrap gap-3">
              {sub.is_founder ? (
                <p
                  className="text-xs text-muted-foreground"
                  data-testid="text-founder-notice"
                >
                  Founder accounts bypass billing entirely. There's nothing to manage here.
                </p>
              ) : sub.stripe_customer_id ? (
                <button
                  type="button"
                  onClick={openPortal}
                  disabled={portalLoading}
                  className="bg-primary text-primary-foreground px-4 py-2 text-xs uppercase tracking-widest font-bold hover:opacity-90 disabled:opacity-50"
                  data-testid="button-open-portal"
                >
                  {portalLoading
                    ? "Opening Stripe…"
                    : "Manage subscription in Stripe"}
                </button>
              ) : (
                <Link
                  href="/pricing"
                  className="bg-primary text-primary-foreground px-4 py-2 text-xs uppercase tracking-widest font-bold hover:opacity-90"
                  data-testid="link-upgrade"
                >
                  Choose a plan
                </Link>
              )}
              {sub.tier !== "founder" && (
                <Link
                  href="/pricing"
                  className="border border-border px-4 py-2 text-xs uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary"
                  data-testid="link-change-plan"
                >
                  Change plan
                </Link>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
