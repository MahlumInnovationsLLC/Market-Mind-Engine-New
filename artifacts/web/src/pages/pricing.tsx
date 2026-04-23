import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearch, useLocation } from "wouter";
import { useAuth, SignInButton } from "@clerk/react";
import {
  ApiError,
  PricingTier,
  useGetBillingPricing,
  useCreateBillingCheckoutSession,
} from "@workspace/api-client-react";
import { PublicFooter, PublicNav } from "@/components/public/public-nav";
import {
  getPendingCheckoutPriceId,
  setPendingCheckoutPriceId,
} from "@/lib/pending-checkout";

export default function PricingPage() {
  const { isSignedIn } = useAuth();
  const search = useSearch();
  const [, setLocation] = useLocation();
  const autoCheckoutPriceId = useMemo(() => {
    const params = new URLSearchParams(search);
    return params.get("checkout");
  }, [search]);
  const autoCheckoutFired = useRef(false);
  const [interval, setInterval] = useState<"month" | "year">("month");
  const [actionTier, setActionTier] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [autoCheckoutError, setAutoCheckoutError] = useState<string | null>(
    null,
  );
  const [pendingPriceId, setPendingPriceId] = useState<string | null>(() =>
    typeof window !== "undefined" ? getPendingCheckoutPriceId() : null,
  );
  const pendingIntervalApplied = useRef(false);

  const dismissAutoCheckout = () => {
    autoCheckoutFired.current = false;
    setAutoCheckoutError(null);
    setActionError(null);
    setLocation("/pricing");
  };

  const retryAutoCheckout = async () => {
    if (!autoCheckoutPriceId || !isSignedIn) return;
    const target = catalog?.tiers.find(
      (t) =>
        t.monthly?.price_id === autoCheckoutPriceId ||
        t.annual?.price_id === autoCheckoutPriceId,
    );
    if (!target) {
      setAutoCheckoutError(
        "That pricing link is no longer valid. Pick a tier below to continue.",
      );
      return;
    }
    setAutoCheckoutError(null);
    setActionError(null);
    await startCheckout(target.id, autoCheckoutPriceId);
  };

  const pricingQuery = useGetBillingPricing();
  const catalog = pricingQuery.data ?? null;
  const loadError = pricingQuery.error
    ? pricingQuery.error instanceof Error
      ? pricingQuery.error.message
      : String(pricingQuery.error)
    : null;

  const checkoutMutation = useCreateBillingCheckoutSession();

  // Public-facing tier comparison is strictly Free / Pro / Enterprise.
  // The internal `founder` tier is excluded from the public grid (it's
  // resolved server-side via the operator allow-list and bypasses
  // billing). The `elite` tier is presented as "Enterprise" copy.
  const tiers = (catalog?.tiers ?? [])
    .filter((t) => t.id !== "founder")
    .map((t) =>
      t.id === "elite" ? { ...t, name: "Enterprise" } : t,
    );

  const startCheckout = async (tierId: string, priceId: string) => {
    setActionTier(tierId);
    setActionError(null);
    // Persist the chosen price so we can offer "Resume checkout" if Stripe
    // throws an error or the user hits "Back" inside the hosted page. We
    // also mirror it into local state so an in-page failure (no remount)
    // immediately renders the recovery affordance.
    setPendingCheckoutPriceId(priceId);
    setPendingPriceId(priceId);
    try {
      const res = await checkoutMutation.mutateAsync({
        data: { price_id: priceId },
      });
      if (res.url) {
        window.location.href = res.url;
        return;
      }
      const noUrlMsg = "Stripe did not return a checkout URL.";
      if (autoCheckoutPriceId === priceId) {
        setAutoCheckoutError(noUrlMsg);
      } else {
        setActionError(noUrlMsg);
      }
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `Checkout failed (HTTP ${e.status})`
          : e instanceof Error
            ? e.message
            : String(e);
      if (autoCheckoutPriceId === priceId) {
        setAutoCheckoutError(msg);
      } else {
        setActionError(msg);
      }
    } finally {
      setActionTier(null);
    }
  };

  // Tier-aware CTA handoff from the marketing page (`/pricing?checkout=<price_id>`).
  // After Clerk sign-up the user lands here; if they're authenticated and the
  // requested price_id matches a configured slot we go straight to Stripe.
  useEffect(() => {
    if (autoCheckoutFired.current) return;
    if (!autoCheckoutPriceId || !catalog || !isSignedIn) return;
    const target = catalog.tiers.find(
      (t) =>
        t.monthly?.price_id === autoCheckoutPriceId ||
        t.annual?.price_id === autoCheckoutPriceId,
    );
    if (!target) {
      autoCheckoutFired.current = true;
      setAutoCheckoutError(
        "That pricing link is no longer valid. Pick a tier below to continue.",
      );
      return;
    }
    autoCheckoutFired.current = true;
    if (target.annual?.price_id === autoCheckoutPriceId) {
      setInterval("year");
    } else {
      setInterval("month");
    }
    void startCheckout(target.id, autoCheckoutPriceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoCheckoutPriceId, catalog, isSignedIn]);

  // Resume-checkout: if the user previously kicked off a checkout and bailed,
  // auto-switch the billing interval toggle to match the stored price so the
  // highlighted card and "Resume checkout" button line up on first paint.
  useEffect(() => {
    if (pendingIntervalApplied.current) return;
    if (!pendingPriceId || !catalog) return;
    const target = catalog.tiers.find(
      (t) =>
        t.monthly?.price_id === pendingPriceId ||
        t.annual?.price_id === pendingPriceId,
    );
    if (!target) return;
    pendingIntervalApplied.current = true;
    if (target.annual?.price_id === pendingPriceId) {
      setInterval("year");
    } else {
      setInterval("month");
    }
  }, [pendingPriceId, catalog]);

  const handleUpgrade = async (tier: PricingTier) => {
    setActionError(null);
    const slot = interval === "month" ? tier.monthly : tier.annual;
    if (!slot?.price_id) {
      setActionError(
        `${tier.name} ${interval === "month" ? "monthly" : "annual"} pricing is not yet configured.`,
      );
      return;
    }
    if (!isSignedIn) {
      setActionError("Please sign in to upgrade.");
      return;
    }
    await startCheckout(tier.id, slot.price_id);
  };

  const showCheckoutSplash =
    !!autoCheckoutPriceId && isSignedIn && !autoCheckoutError;
  const showCheckoutErrorSplash =
    !!autoCheckoutPriceId && isSignedIn && !!autoCheckoutError;

  const priceLabel = (tier: PricingTier) => {
    const amount = interval === "month" ? tier.monthly_usd : tier.annual_usd;
    if (amount <= 0) return "$0";
    return `$${amount}`;
  };

  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-sans selection:bg-primary/30">
      <PublicNav active="pricing" />

      <header className="relative bg-starfield overflow-hidden border-b border-border/60">
        <div className="absolute inset-0 grid-bg opacity-40 pointer-events-none" />
        <div className="relative max-w-6xl mx-auto px-4 md:px-8 pt-16 md:pt-20 pb-12 text-center">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-primary mb-3">
            Pricing
          </div>
          <h1
            className="text-3xl md:text-5xl font-extrabold tracking-tight"
            data-testid="text-pricing-title"
          >
            Pick a tier.{" "}
            <span className="text-gradient-brand">Cancel anytime.</span>
          </h1>
          <p className="text-muted-foreground text-base mt-4 max-w-xl mx-auto">
            Live trading telemetry for prediction markets — full methodology and
            real Kalshi fills, every cycle.
          </p>
          {isSignedIn && (
            <div className="mt-6">
              <Link
                href="/profile"
                className="inline-flex items-center gap-1.5 rounded-full border border-border px-4 py-1.5 text-sm text-muted-foreground hover:border-primary/60 hover:text-primary transition-colors"
                data-testid="link-billing"
              >
                Manage your billing →
              </Link>
            </div>
          )}
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-4 md:px-8 pt-12 pb-20">
        {showCheckoutSplash ? (
          <div
            className="max-w-md mx-auto text-center py-16"
            data-testid="splash-checkout-redirect"
          >
            <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-primary/10 border border-primary/30 mb-6">
              <svg
                className="w-8 h-8 text-primary animate-spin"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                aria-hidden
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
                />
              </svg>
            </div>
            <h2 className="text-2xl font-extrabold tracking-tight">
              Redirecting to{" "}
              <span className="text-gradient-brand">Stripe Checkout</span>…
            </h2>
            <p className="text-muted-foreground text-sm mt-3">
              Hang tight — we're handing you off to our secure payment partner
              to finish your sign-up.
            </p>
            <button
              type="button"
              onClick={dismissAutoCheckout}
              className="mt-8 text-xs uppercase tracking-widest text-muted-foreground hover:text-primary transition-colors"
              data-testid="button-cancel-redirect"
            >
              Cancel and pick a different tier
            </button>
          </div>
        ) : showCheckoutErrorSplash ? (
          <div
            className="max-w-md mx-auto text-center py-16"
            data-testid="splash-checkout-error"
          >
            <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-destructive/10 border border-destructive/40 text-destructive mb-6">
              <svg
                className="w-8 h-8"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                aria-hidden
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"
                />
              </svg>
            </div>
            <h2 className="text-2xl font-extrabold tracking-tight">
              We couldn't start your checkout
            </h2>
            <p
              className="text-muted-foreground text-sm mt-3"
              data-testid="text-checkout-error-message"
            >
              {autoCheckoutError}
            </p>
            <div className="mt-8 flex flex-col sm:flex-row gap-3 justify-center">
              <button
                type="button"
                onClick={retryAutoCheckout}
                disabled={actionTier !== null}
                className="inline-flex items-center justify-center rounded-full bg-primary text-primary-foreground px-5 py-3 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary disabled:opacity-60"
                data-testid="button-retry-checkout"
              >
                {actionTier !== null ? "Retrying…" : "Try again"}
              </button>
              <button
                type="button"
                onClick={dismissAutoCheckout}
                className="inline-flex items-center justify-center rounded-full border border-border px-5 py-3 text-sm font-semibold text-foreground hover:border-primary/60 hover:text-primary transition-colors"
                data-testid="button-pick-different-tier"
              >
                Pick a different tier
              </button>
            </div>
          </div>
        ) : (
        <>
        <div className="flex items-center justify-center mb-10">
          <div
            className="inline-flex rounded-full border border-border bg-card/60 p-1"
            role="group"
          >
            <button
              type="button"
              onClick={() => setInterval("month")}
              className={`px-5 py-1.5 text-sm font-semibold rounded-full transition-colors ${
                interval === "month"
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
              data-testid="button-interval-month"
            >
              Monthly
            </button>
            <button
              type="button"
              onClick={() => setInterval("year")}
              className={`px-5 py-1.5 text-sm font-semibold rounded-full transition-colors ${
                interval === "year"
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
              data-testid="button-interval-year"
            >
              Annual <span className="opacity-60 ml-1">(save ~17%)</span>
            </button>
          </div>
        </div>

        {loadError && (
          <div
            className="rounded-xl border border-destructive/60 bg-destructive/10 text-destructive p-4 mb-6 text-sm"
            data-testid="text-load-error"
          >
            Pricing failed to load: {loadError}
          </div>
        )}
        {actionError && (
          <div
            className="rounded-xl border border-destructive/60 bg-destructive/10 text-destructive p-4 mb-6 text-sm"
            data-testid="text-action-error"
          >
            {actionError}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-3 gap-5 max-w-5xl mx-auto">
          {tiers.map((tier) => {
            const slot = interval === "month" ? tier.monthly : tier.annual;
            const isPaid = tier.id === "pro" || tier.id === "elite";
            const configured = slot?.configured ?? false;
            const isPendingTier =
              !!pendingPriceId &&
              (tier.monthly?.price_id === pendingPriceId ||
                tier.annual?.price_id === pendingPriceId);
            const showResume = isPendingTier && isPaid && configured;
            return (
              <div
                key={tier.id}
                className={`rounded-2xl border bg-card p-6 flex flex-col ${
                  showResume
                    ? "border-primary shadow-[0_0_60px_-15px_hsl(189_94%_55%_/_0.6)]"
                    : tier.highlight
                      ? "border-primary/50 shadow-[0_0_60px_-15px_hsl(189_94%_55%_/_0.4)]"
                      : "border-border"
                }`}
                data-testid={`card-tier-${tier.id}`}
              >
                <div className="mb-3">
                  <div className="flex items-center justify-between">
                    <h2 className="text-lg font-semibold text-foreground">
                      {tier.name}
                    </h2>
                    {showResume ? (
                      <span
                        className="text-[10px] uppercase tracking-widest font-semibold rounded-full bg-primary/15 text-primary border border-primary/40 px-2.5 py-1"
                        data-testid={`badge-resume-${tier.id}`}
                      >
                        Your pick
                      </span>
                    ) : tier.highlight ? (
                      <span className="text-[10px] uppercase tracking-widest font-semibold rounded-full bg-primary/10 text-primary border border-primary/30 px-2.5 py-1">
                        Popular
                      </span>
                    ) : null}
                  </div>
                  <p className="text-sm text-muted-foreground mt-1.5 min-h-[40px]">
                    {tier.tagline}
                  </p>
                </div>
                <div className="mb-5">
                  <div
                    className="text-3xl font-extrabold text-foreground"
                    data-testid={`text-price-${tier.id}`}
                  >
                    {priceLabel(tier)}
                    {tier.monthly_usd > 0 && (
                      <span className="text-sm text-muted-foreground ml-1.5 font-normal">
                        /{interval === "month" ? "mo" : "yr"}
                      </span>
                    )}
                  </div>
                </div>
                <ul className="text-sm text-foreground/80 space-y-2.5 mb-6 flex-1">
                  {tier.features.map((f) => (
                    <li key={f} className="flex gap-2.5">
                      <span className="text-primary mt-0.5" aria-hidden>
                        ✓
                      </span>
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>
                {isPaid ? (
                  isSignedIn ? (
                    <button
                      type="button"
                      disabled={!configured || actionTier === tier.id}
                      onClick={() => handleUpgrade(tier)}
                      className={`w-full inline-flex items-center justify-center rounded-full px-5 py-3 text-sm font-semibold transition-all ${
                        configured
                          ? "bg-primary text-primary-foreground hover:opacity-90 glow-primary"
                          : "border border-border text-muted-foreground cursor-not-allowed"
                      }`}
                      data-testid={`button-upgrade-${tier.id}`}
                    >
                      {actionTier === tier.id
                        ? "Redirecting…"
                        : configured
                          ? showResume
                            ? "Resume checkout"
                            : tier.cta
                          : "Coming soon"}
                    </button>
                  ) : (
                    <SignInButton mode="modal">
                      <button
                        type="button"
                        className="w-full inline-flex items-center justify-center rounded-full bg-primary text-primary-foreground px-5 py-3 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary"
                        data-testid={`button-signin-${tier.id}`}
                      >
                        Sign in to upgrade
                      </button>
                    </SignInButton>
                  )
                ) : (
                  <Link
                    href={tier.id === "free" ? "/" : "/"}
                    className="w-full inline-flex items-center justify-center rounded-full border border-border px-5 py-3 text-sm font-semibold text-foreground hover:border-primary/60 hover:text-primary transition-colors"
                    data-testid={`button-cta-${tier.id}`}
                  >
                    {tier.cta}
                  </Link>
                )}
              </div>
            );
          })}
        </div>

        {catalog && !catalog.stripe_configured && (
          <p
            className="text-xs text-muted-foreground text-center mt-10"
            data-testid="text-stripe-warning"
          >
            Stripe is not yet fully configured — checkout will be unavailable
            until the operator adds the secret key.
          </p>
        )}
        </>
        )}
      </div>

      <PublicFooter />
    </div>
  );
}
