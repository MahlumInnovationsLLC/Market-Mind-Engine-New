import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearch } from "wouter";
import { useAuth, SignInButton } from "@clerk/react";
import {
  ApiError,
  PricingTier,
  useGetBillingPricing,
  useCreateBillingCheckoutSession,
} from "@workspace/api-client-react";
import { PublicFooter, PublicNav } from "@/components/public/public-nav";

export default function PricingPage() {
  const { isSignedIn } = useAuth();
  const search = useSearch();
  const autoCheckoutPriceId = useMemo(() => {
    const params = new URLSearchParams(search);
    return params.get("checkout");
  }, [search]);
  const autoCheckoutFired = useRef(false);
  const [interval, setInterval] = useState<"month" | "year">("month");
  const [actionTier, setActionTier] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

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
    try {
      const res = await checkoutMutation.mutateAsync({
        data: { price_id: priceId },
      });
      if (res.url) {
        window.location.href = res.url;
        return;
      }
      setActionError("Stripe did not return a checkout URL.");
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `Checkout failed (HTTP ${e.status})`
          : e instanceof Error
            ? e.message
            : String(e);
      setActionError(msg);
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
    if (!target) return;
    autoCheckoutFired.current = true;
    if (target.annual?.price_id === autoCheckoutPriceId) {
      setInterval("year");
    } else {
      setInterval("month");
    }
    void startCheckout(target.id, autoCheckoutPriceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoCheckoutPriceId, catalog, isSignedIn]);

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

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
          {tiers.map((tier) => {
            const slot = interval === "month" ? tier.monthly : tier.annual;
            const isPaid = tier.id === "pro" || tier.id === "elite";
            const configured = slot?.configured ?? false;
            return (
              <div
                key={tier.id}
                className={`rounded-2xl border bg-card p-6 flex flex-col ${
                  tier.highlight
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
                    {tier.highlight && (
                      <span className="text-[10px] uppercase tracking-widest font-semibold rounded-full bg-primary/10 text-primary border border-primary/30 px-2.5 py-1">
                        Popular
                      </span>
                    )}
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
                          ? tier.cta
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
      </div>

      <PublicFooter />
    </div>
  );
}
