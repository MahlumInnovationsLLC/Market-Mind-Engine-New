import { useState } from "react";
import { Link, useLocation } from "wouter";
import { useAuth, useClerk } from "@clerk/react";
import {
  ApiError,
  UserSubscription,
  UserSubscriptionTier,
  getGetMySubscriptionQueryKey,
  useCreateBillingPortalSession,
  useGetMySubscription,
} from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";

const TIER_COPY: Record<UserSubscription["tier"], string> = {
  [UserSubscriptionTier.free]: "Free — read-only dashboard at delay.",
  [UserSubscriptionTier.pro]: "Pro — live signals + full swarm transcripts.",
  [UserSubscriptionTier.elite]: "Elite — Pro + institutional features.",
  [UserSubscriptionTier.founder]:
    "Founder — operator allow-list, billing bypassed.",
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

export default function BillingPage() {
  const { isSignedIn } = useAuth();
  const { signOut } = useClerk();
  const [location] = useLocation();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const showSuccess = location.includes("checkout=success");

  const subQuery = useGetMySubscription({
    query: {
      enabled: !!isSignedIn,
      queryKey: getGetMySubscriptionQueryKey(),
    },
  });
  const sub = subQuery.data ?? null;
  const loading = subQuery.isLoading;
  const fetchError = subQuery.error
    ? subQuery.error instanceof Error
      ? subQuery.error.message
      : String(subQuery.error)
    : null;

  const portalMutation = useCreateBillingPortalSession();
  const portalLoading = portalMutation.isPending;

  const openPortal = async () => {
    setError(null);
    try {
      const res = await portalMutation.mutateAsync();
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
    }
  };

  const refreshSubscription = () => {
    void queryClient.invalidateQueries({
      queryKey: getGetMySubscriptionQueryKey(),
    });
  };

  const displayedError = error ?? fetchError;

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
              onClick={refreshSubscription}
              className="ml-3 underline"
              data-testid="button-refresh-after-checkout"
            >
              Refresh
            </button>
          </div>
        )}

        {displayedError && (
          <div
            className="border border-destructive text-destructive p-3 text-xs uppercase tracking-widest"
            data-testid="text-error"
          >
            {displayedError}
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
