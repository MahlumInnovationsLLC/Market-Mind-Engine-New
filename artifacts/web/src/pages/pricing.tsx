import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { useAuth, useClerk, SignInButton } from "@clerk/react";
import { apiFetch, ApiError } from "@/lib/api";

interface PriceSlot {
  price_id: string | null;
  configured: boolean;
}

interface PricingTier {
  id: "free" | "pro" | "elite" | "founder";
  name: string;
  tagline: string;
  monthly_usd: number;
  annual_usd: number;
  features: string[];
  monthly: PriceSlot | null;
  annual: PriceSlot | null;
  cta: string;
  highlight?: boolean;
}

interface PricingCatalog {
  tiers: PricingTier[];
  publishable_key_present: boolean;
  stripe_configured: boolean;
}

export default function PricingPage() {
  const { isSignedIn, getToken } = useAuth();
  const [catalog, setCatalog] = useState<PricingCatalog | null>(null);
  const [interval, setInterval] = useState<"month" | "year">("month");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionTier, setActionTier] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    apiFetch<PricingCatalog>("/api/billing/pricing")
      .then((data) => {
        if (!cancel) setCatalog(data);
      })
      .catch((e) => {
        if (!cancel) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancel = true;
    };
  }, []);

  const tiers = catalog?.tiers ?? [];

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
    setActionTier(tier.id);
    try {
      const res = await apiFetch<{ url: string }>(
        "/api/billing/checkout-session",
        {
          method: "POST",
          body: { price_id: slot.price_id },
          getToken: async () => (await getToken()) ?? null,
        },
      );
      if (res.url) {
        window.location.href = res.url;
      } else {
        setActionError("Stripe did not return a checkout URL.");
      }
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

  const priceLabel = (tier: PricingTier) => {
    const amount = interval === "month" ? tier.monthly_usd : tier.annual_usd;
    if (amount <= 0) return "$0";
    return `$${amount}`;
  };

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-8 font-mono selection:bg-primary/30">
      <header className="max-w-6xl mx-auto mb-8 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1
            className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase"
            data-testid="text-pricing-title"
          >
            AXIOM_PREDICT :: PRICING
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            Live trading telemetry for prediction markets
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
          {isSignedIn && (
            <Link
              href="/billing"
              className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
              data-testid="link-billing"
            >
              My Billing
            </Link>
          )}
        </div>
      </header>

      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-center mb-8">
          <div className="inline-flex border border-border" role="group">
            <button
              type="button"
              onClick={() => setInterval("month")}
              className={`px-4 py-2 text-xs uppercase tracking-widest ${
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
              className={`px-4 py-2 text-xs uppercase tracking-widest border-l border-border ${
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
            className="border border-destructive text-destructive p-3 mb-6 text-xs uppercase tracking-widest"
            data-testid="text-load-error"
          >
            Pricing failed to load: {loadError}
          </div>
        )}
        {actionError && (
          <div
            className="border border-destructive text-destructive p-3 mb-6 text-xs uppercase tracking-widest"
            data-testid="text-action-error"
          >
            {actionError}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {tiers.map((tier) => {
            const slot = interval === "month" ? tier.monthly : tier.annual;
            const isPaid = tier.id === "pro" || tier.id === "elite";
            const configured = slot?.configured ?? false;
            return (
              <div
                key={tier.id}
                className={`border ${tier.highlight ? "border-primary" : "border-border"} bg-card p-5 flex flex-col`}
                data-testid={`card-tier-${tier.id}`}
              >
                <div className="mb-3">
                  <div className="flex items-center justify-between">
                    <h2 className="text-sm uppercase tracking-widest text-foreground font-bold">
                      {tier.name}
                    </h2>
                    {tier.highlight && (
                      <span className="text-[10px] uppercase tracking-widest text-primary border border-primary px-1">
                        Popular
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground mt-1 min-h-[32px]">
                    {tier.tagline}
                  </p>
                </div>
                <div className="mb-4">
                  <div
                    className="text-2xl text-primary font-bold"
                    data-testid={`text-price-${tier.id}`}
                  >
                    {priceLabel(tier)}
                    {tier.monthly_usd > 0 && (
                      <span className="text-xs text-muted-foreground ml-1">
                        /{interval === "month" ? "mo" : "yr"}
                      </span>
                    )}
                  </div>
                </div>
                <ul className="text-xs text-foreground/80 space-y-1 mb-5 flex-1">
                  {tier.features.map((f) => (
                    <li key={f} className="flex gap-2">
                      <span className="text-primary">›</span>
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
                      className={`w-full py-2 text-xs uppercase tracking-widest font-bold border ${
                        configured
                          ? "bg-primary text-primary-foreground border-primary hover:opacity-90"
                          : "border-border text-muted-foreground cursor-not-allowed"
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
                        className="w-full py-2 text-xs uppercase tracking-widest font-bold bg-primary text-primary-foreground border border-primary hover:opacity-90"
                        data-testid={`button-signin-${tier.id}`}
                      >
                        Sign in to upgrade
                      </button>
                    </SignInButton>
                  )
                ) : (
                  <Link
                    href={tier.id === "free" ? "/" : "/"}
                    className="w-full py-2 text-xs uppercase tracking-widest font-bold border border-border text-muted-foreground hover:text-foreground text-center"
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
            className="text-[10px] text-muted-foreground uppercase tracking-widest text-center mt-8"
            data-testid="text-stripe-warning"
          >
            Stripe is not yet fully configured — checkout will be unavailable until the operator adds the secret key.
          </p>
        )}
      </div>
    </div>
  );
}
