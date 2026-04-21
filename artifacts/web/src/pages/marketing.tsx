import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { SignInButton, SignUpButton, useAuth } from "@clerk/react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiFetch } from "@/lib/api";

type RangeKey = "7" | "30" | "90";

interface EquityPoint {
  date: string;
  daily_pnl: number;
  cumulative_pnl: number;
  trades: number;
}

interface EquityCurveResponse {
  days: number;
  points: EquityPoint[];
}

interface PublicStats {
  realized_pnl: number | null;
  total_trades: number | null;
  win_rate: number | null;
  last_trade_at: string | null;
  trading_cycles: number | null;
  swarm_debates: number | null;
}

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
}

const RANGE_LABEL: Record<RangeKey, string> = {
  "7": "7D",
  "30": "30D",
  "90": "90D",
};

function formatUsd(n: number | null | undefined, signed = false): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const formatted =
    abs >= 1000
      ? `$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
      : `$${abs.toFixed(2)}`;
  if (!signed) return n < 0 ? `-${formatted}` : formatted;
  return n < 0 ? `-${formatted}` : `+${formatted}`;
}

function formatPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return Math.round(n).toLocaleString();
}

export default function MarketingPage() {
  const { isSignedIn } = useAuth();
  const [range, setRange] = useState<RangeKey>("30");
  const [curve, setCurve] = useState<EquityPoint[]>([]);
  const [curveLoading, setCurveLoading] = useState(true);
  const [curveError, setCurveError] = useState<string | null>(null);
  const [stats, setStats] = useState<PublicStats | null>(null);
  const [catalog, setCatalog] = useState<PricingCatalog | null>(null);

  useEffect(() => {
    let cancel = false;
    setCurveLoading(true);
    setCurveError(null);
    apiFetch<EquityCurveResponse>(`/api/public/equity-curve?days=${range}`)
      .then((res) => {
        if (cancel) return;
        setCurve(res.points);
      })
      .catch((e) => {
        if (cancel) return;
        setCurveError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancel) setCurveLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [range]);

  useEffect(() => {
    let cancel = false;
    apiFetch<PublicStats>("/api/public/stats")
      .then((data) => {
        if (!cancel) setStats(data);
      })
      .catch(() => {
        // Hero stats are best-effort — leave them blank on failure.
      });
    apiFetch<PricingCatalog>("/api/billing/pricing")
      .then((data) => {
        if (!cancel) setCatalog(data);
      })
      .catch(() => {
        // Pricing block falls back to a static placeholder.
      });
    return () => {
      cancel = true;
    };
  }, []);

  const lastPoint = curve.length > 0 ? curve[curve.length - 1] : null;
  const firstPoint = curve.length > 0 ? curve[0] : null;
  const periodPnl =
    lastPoint && firstPoint
      ? lastPoint.cumulative_pnl - firstPoint.cumulative_pnl + firstPoint.daily_pnl
      : null;
  const periodTrades = useMemo(
    () => curve.reduce((acc, p) => acc + p.trades, 0),
    [curve],
  );

  const paidTiers = (catalog?.tiers ?? []).filter(
    (t) => t.id === "pro" || t.id === "elite",
  );

  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-mono selection:bg-primary/30">
      {/* ---------- Top Nav ---------- */}
      <nav className="border-b border-border">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-3 flex items-center justify-between">
          <Link
            href="/"
            className="flex items-center gap-3"
            data-testid="link-home-logo"
          >
            <div className="h-8 w-8 border border-primary text-primary flex items-center justify-center text-[10px] font-bold tracking-widest">
              AX
            </div>
            <div className="text-xs uppercase tracking-widest text-foreground">
              Axiom_Predict
            </div>
          </Link>
          <div className="flex items-center gap-2 md:gap-4 text-xs">
            <a
              href="#methodology"
              className="hidden md:inline text-muted-foreground hover:text-primary uppercase tracking-widest"
            >
              Methodology
            </a>
            <a
              href="#equity"
              className="hidden md:inline text-muted-foreground hover:text-primary uppercase tracking-widest"
            >
              Live Curve
            </a>
            <a
              href="#pricing"
              className="hidden md:inline text-muted-foreground hover:text-primary uppercase tracking-widest"
            >
              Pricing
            </a>
            {isSignedIn ? (
              <Link
                href="/app"
                className="border border-primary text-primary px-3 py-1 uppercase tracking-widest text-[10px] hover:bg-primary hover:text-primary-foreground"
                data-testid="link-open-console"
              >
                Open Console →
              </Link>
            ) : (
              <>
                <SignInButton mode="modal">
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-primary uppercase tracking-widest"
                    data-testid="button-nav-signin"
                  >
                    Sign in
                  </button>
                </SignInButton>
                <SignUpButton mode="modal">
                  <button
                    type="button"
                    className="border border-primary text-primary px-3 py-1 uppercase tracking-widest text-[10px] hover:bg-primary hover:text-primary-foreground"
                    data-testid="button-nav-signup"
                  >
                    Get Access
                  </button>
                </SignUpButton>
              </>
            )}
          </div>
        </div>
      </nav>

      {/* ---------- Hero ---------- */}
      <section className="border-b border-border">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-16 md:py-24">
          <div className="text-[10px] text-primary uppercase tracking-widest mb-4">
            ::SYS_ONLINE — live prediction-market trading
          </div>
          <h1 className="text-3xl md:text-5xl font-bold uppercase tracking-tight text-foreground max-w-3xl">
            A six-persona LLM swarm trading{" "}
            <span className="text-primary">prediction markets</span> with
            twelve-gate risk control.
          </h1>
          <p className="text-sm md:text-base text-muted-foreground mt-6 max-w-2xl">
            Axiom-Predict runs a continuous research-debate-execute loop on
            Kalshi. Six analyst personas argue every pick, twelve risk gates
            decide what gets sized, and the entire transcript — including this
            equity curve — is yours to inspect.
          </p>
          <div className="flex flex-wrap gap-3 mt-8">
            {isSignedIn ? (
              <Link
                href="/app"
                className="bg-primary text-primary-foreground px-5 py-3 uppercase tracking-widest text-xs font-bold hover:opacity-90"
                data-testid="link-hero-console"
              >
                Open Operator Console →
              </Link>
            ) : (
              <SignUpButton mode="modal">
                <button
                  type="button"
                  className="bg-primary text-primary-foreground px-5 py-3 uppercase tracking-widest text-xs font-bold hover:opacity-90"
                  data-testid="button-hero-signup"
                >
                  Get Access →
                </button>
              </SignUpButton>
            )}
            <a
              href="#equity"
              className="border border-border px-5 py-3 uppercase tracking-widest text-xs text-muted-foreground hover:text-primary hover:border-primary"
            >
              See Live Performance
            </a>
          </div>

          {/* Hero stat strip */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-px mt-12 border border-border bg-border">
            <HeroStat
              label="Realized PnL"
              value={formatUsd(stats?.realized_pnl)}
              testId="stat-realized-pnl"
              accent={
                stats?.realized_pnl !== null && stats?.realized_pnl !== undefined
                  ? stats.realized_pnl >= 0
                    ? "positive"
                    : "negative"
                  : undefined
              }
            />
            <HeroStat
              label="Win Rate"
              value={formatPct(stats?.win_rate)}
              testId="stat-win-rate"
            />
            <HeroStat
              label="Trades Logged"
              value={formatInt(stats?.total_trades)}
              testId="stat-trades"
            />
            <HeroStat
              label="Swarm Debates"
              value={formatInt(stats?.swarm_debates)}
              testId="stat-debates"
            />
          </div>
        </div>
      </section>

      {/* ---------- Methodology ---------- */}
      <section id="methodology" className="border-b border-border">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-16">
          <SectionHeader
            kicker="01 :: methodology"
            title="No black box. Every pick is debated, gated, and logged."
          />
          <div className="grid grid-cols-1 md:grid-cols-3 gap-px bg-border border border-border mt-10">
            <MethodCard
              tag="THE SWARM"
              title="Six-persona debate"
              body="Six analyst personas — quantitative, contrarian, momentum, fundamentals, sentiment, and a designated devil's advocate — independently price every market and then argue. Calibration metrics for each persona are tracked and weighted over time."
            />
            <MethodCard
              tag="THE GATES"
              title="Twelve-gate risk control"
              body="Before any size hits the book a candidate trade clears twelve sequential gates: liquidity, spread, slippage, edge vs. fee, exposure caps, drawdown, kill-switch, persona disagreement bands, and more. A single veto kills the order."
            />
            <MethodCard
              tag="THE LOOP"
              title="Continuous research → execute"
              body="A scheduler runs trading cycles 24/7 on Kalshi. Every cycle's full transcript — pick, debate, gate verdicts, fill, settlement, post-mortem — is persisted and queryable in your console."
            />
          </div>
        </div>
      </section>

      {/* ---------- Live Equity Curve ---------- */}
      <section id="equity" className="border-b border-border bg-card">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-16">
          <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 mb-6">
            <SectionHeader
              kicker="02 :: live equity curve"
              title="Real fills. Real PnL. Updated every cycle."
              compact
            />
            <div
              className="inline-flex border border-border self-start md:self-auto"
              role="group"
            >
              {(["7", "30", "90"] as RangeKey[]).map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRange(r)}
                  className={`px-4 py-2 text-[10px] uppercase tracking-widest ${
                    range === r
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  } ${r !== "7" ? "border-l border-border" : ""}`}
                  data-testid={`button-range-${r}`}
                >
                  {RANGE_LABEL[r]}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-3 gap-px border border-border bg-border mb-6">
            <HeroStat
              label={`${RANGE_LABEL[range]} PnL`}
              value={formatUsd(periodPnl, true)}
              testId="stat-period-pnl"
              accent={
                periodPnl !== null
                  ? periodPnl >= 0
                    ? "positive"
                    : "negative"
                  : undefined
              }
            />
            <HeroStat
              label={`${RANGE_LABEL[range]} Trades`}
              value={formatInt(periodTrades)}
              testId="stat-period-trades"
            />
            <HeroStat
              label="Cumulative PnL"
              value={formatUsd(lastPoint?.cumulative_pnl ?? null)}
              testId="stat-cumulative-pnl"
              accent={
                lastPoint && lastPoint.cumulative_pnl >= 0
                  ? "positive"
                  : lastPoint
                    ? "negative"
                    : undefined
              }
            />
          </div>

          <div
            className="border border-border bg-background h-[320px] p-2"
            data-testid="chart-equity-curve"
          >
            {curveLoading ? (
              <div className="h-full flex items-center justify-center text-xs uppercase tracking-widest text-muted-foreground">
                LOADING…
              </div>
            ) : curveError ? (
              <div className="h-full flex items-center justify-center text-xs uppercase tracking-widest text-destructive p-4 text-center">
                Equity curve unavailable: {curveError}
              </div>
            ) : curve.length === 0 ? (
              <div className="h-full flex items-center justify-center text-xs uppercase tracking-widest text-muted-foreground">
                No trades in the selected window yet.
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={curve}
                  margin={{ top: 10, right: 16, left: 0, bottom: 0 }}
                >
                  <defs>
                    <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                      <stop
                        offset="0%"
                        stopColor="hsl(188, 86%, 53%)"
                        stopOpacity={0.4}
                      />
                      <stop
                        offset="100%"
                        stopColor="hsl(188, 86%, 53%)"
                        stopOpacity={0}
                      />
                    </linearGradient>
                  </defs>
                  <CartesianGrid
                    strokeDasharray="2 4"
                    stroke="hsl(240, 6%, 20%)"
                  />
                  <XAxis
                    dataKey="date"
                    stroke="hsl(240, 5%, 65%)"
                    tick={{ fontSize: 10, fontFamily: "monospace" }}
                    tickFormatter={(v: string) => v.slice(5)}
                    minTickGap={24}
                  />
                  <YAxis
                    stroke="hsl(240, 5%, 65%)"
                    tick={{ fontSize: 10, fontFamily: "monospace" }}
                    tickFormatter={(v: number) => formatUsd(v)}
                    width={70}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "hsl(240, 10%, 6%)",
                      border: "1px solid hsl(240, 6%, 20%)",
                      borderRadius: 0,
                      fontSize: 11,
                      fontFamily: "monospace",
                    }}
                    labelStyle={{ color: "hsl(0, 0%, 95%)" }}
                    formatter={(value: number, name: string) => [
                      formatUsd(value, name === "daily_pnl"),
                      name === "cumulative_pnl" ? "Cumulative" : "Daily",
                    ]}
                  />
                  <Area
                    type="monotone"
                    dataKey="cumulative_pnl"
                    stroke="hsl(188, 86%, 53%)"
                    strokeWidth={2}
                    fill="url(#equityFill)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
          <p className="text-[10px] text-muted-foreground uppercase tracking-widest mt-3">
            Source: live `trades` table — refreshed every trading cycle.
          </p>
        </div>
      </section>

      {/* ---------- Pricing ---------- */}
      <section id="pricing" className="border-b border-border">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-16">
          <SectionHeader
            kicker="03 :: pricing"
            title="Pick a tier. Cancel anytime."
          />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-10">
            {paidTiers.length === 0
              ? [0, 1].map((i) => (
                  <div
                    key={i}
                    className="border border-border bg-card p-6 h-48 animate-pulse"
                  />
                ))
              : paidTiers.map((tier) => (
                  <TierCard key={tier.id} tier={tier} isSignedIn={!!isSignedIn} />
                ))}
          </div>
          <div className="mt-6 text-center">
            <Link
              href="/pricing"
              className="text-xs uppercase tracking-widest text-muted-foreground hover:text-primary"
              data-testid="link-full-pricing"
            >
              Compare all tiers →
            </Link>
          </div>
        </div>
      </section>

      {/* ---------- FAQ ---------- */}
      <section className="border-b border-border bg-card">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-16">
          <SectionHeader kicker="04 :: faq" title="Common questions" />
          <div className="mt-8 grid grid-cols-1 md:grid-cols-2 gap-x-10 gap-y-6">
            <Faq
              q="Is this paper trading or live?"
              a="Live — orders are placed against Kalshi in real money mode. The equity curve above is real fills. A `paper` mode exists for backtesting but is not what's plotted here."
            />
            <Faq
              q="Do I get to see the swarm's reasoning?"
              a="Yes. Pro and Elite tiers stream the full LLM transcript per pick — every persona's argument, the gate verdicts, and the post-mortem when the market settles."
            />
            <Faq
              q="Can I trade my own account?"
              a="The console is read-only telemetry on the operator's book. Subscribers don't get order routing — they get the signals, the methodology, and the receipts."
            />
            <Faq
              q="What if performance drops?"
              a="You can cancel any time from the billing portal. There is no contract."
            />
          </div>
        </div>
      </section>

      {/* ---------- Final CTA ---------- */}
      <section className="border-b border-border">
        <div className="max-w-4xl mx-auto px-4 md:px-6 py-20 text-center">
          <div className="text-[10px] text-primary uppercase tracking-widest mb-3">
            ::ENTRY_POINT
          </div>
          <h2 className="text-2xl md:text-4xl font-bold uppercase tracking-tight">
            Watch a real swarm trade real markets.
          </h2>
          <p className="text-sm text-muted-foreground mt-4">
            Create an account in under a minute. Pick a tier when you're ready.
          </p>
          <div className="mt-8 flex justify-center gap-3">
            {isSignedIn ? (
              <Link
                href="/app"
                className="bg-primary text-primary-foreground px-6 py-3 uppercase tracking-widest text-xs font-bold hover:opacity-90"
                data-testid="link-cta-console"
              >
                Open Console →
              </Link>
            ) : (
              <SignUpButton mode="modal">
                <button
                  type="button"
                  className="bg-primary text-primary-foreground px-6 py-3 uppercase tracking-widest text-xs font-bold hover:opacity-90"
                  data-testid="button-cta-signup"
                >
                  Create Account →
                </button>
              </SignUpButton>
            )}
          </div>
        </div>
      </section>

      {/* ---------- Footer ---------- */}
      <footer className="py-8">
        <div className="max-w-6xl mx-auto px-4 md:px-6 flex flex-col md:flex-row items-center justify-between gap-4 text-[10px] uppercase tracking-widest text-muted-foreground">
          <div>© Axiom_Predict — operator-grade prediction-market trading</div>
          <div className="flex gap-4">
            <Link
              href="/pricing"
              className="hover:text-primary"
              data-testid="link-footer-pricing"
            >
              Pricing
            </Link>
            <Link
              href="/health"
              className="hover:text-primary"
              data-testid="link-footer-health"
            >
              Status
            </Link>
            {isSignedIn ? (
              <Link
                href="/app"
                className="hover:text-primary"
                data-testid="link-footer-app"
              >
                Console
              </Link>
            ) : (
              <SignInButton mode="modal">
                <button
                  type="button"
                  className="hover:text-primary"
                  data-testid="button-footer-signin"
                >
                  Sign in
                </button>
              </SignInButton>
            )}
          </div>
        </div>
      </footer>
    </div>
  );
}

function TierCard({
  tier,
  isSignedIn,
}: {
  tier: PricingTier;
  isSignedIn: boolean;
}) {
  // Carry the chosen monthly price_id through Clerk sign-up so the user
  // lands on /pricing with `?checkout=<price_id>`, which auto-fires
  // Stripe Checkout. Falls back to the plain pricing page if the slot
  // isn't configured yet.
  const priceId = tier.monthly?.configured ? tier.monthly?.price_id : null;
  const target = priceId
    ? `/pricing?checkout=${encodeURIComponent(priceId)}`
    : "/pricing";

  const cls = `w-full py-3 text-xs uppercase tracking-widest font-bold border text-center ${
    tier.highlight
      ? "bg-primary text-primary-foreground border-primary hover:opacity-90"
      : "border-border text-foreground hover:border-primary hover:text-primary"
  }`;

  return (
    <div
      className={`border ${
        tier.highlight ? "border-primary" : "border-border"
      } bg-card p-6 flex flex-col`}
      data-testid={`card-marketing-tier-${tier.id}`}
    >
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm uppercase tracking-widest font-bold">
          {tier.name}
        </h3>
        {tier.highlight && (
          <span className="text-[10px] uppercase tracking-widest text-primary border border-primary px-1">
            Popular
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground min-h-[32px]">
        {tier.tagline}
      </p>
      <div className="text-3xl text-primary font-bold mt-4">
        ${tier.monthly_usd}
        <span className="text-xs text-muted-foreground ml-1">/mo</span>
      </div>
      <ul className="text-xs text-foreground/80 space-y-1 my-5 flex-1">
        {tier.features.slice(0, 5).map((f) => (
          <li key={f} className="flex gap-2">
            <span className="text-primary">›</span>
            <span>{f}</span>
          </li>
        ))}
      </ul>
      {isSignedIn ? (
        <Link
          href={target}
          className={cls}
          data-testid={`link-marketing-cta-${tier.id}`}
        >
          {tier.cta}
        </Link>
      ) : (
        <SignUpButton
          mode="modal"
          forceRedirectUrl={target}
          signInForceRedirectUrl={target}
        >
          <button
            type="button"
            className={cls}
            data-testid={`button-marketing-cta-${tier.id}`}
          >
            {tier.cta}
          </button>
        </SignUpButton>
      )}
    </div>
  );
}

function HeroStat({
  label,
  value,
  testId,
  accent,
}: {
  label: string;
  value: string;
  testId?: string;
  accent?: "positive" | "negative";
}) {
  const valueColor =
    accent === "positive"
      ? "text-primary"
      : accent === "negative"
        ? "text-destructive"
        : "text-foreground";
  return (
    <div className="bg-background p-4 md:p-5">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div
        className={`text-xl md:text-2xl font-bold mt-2 ${valueColor}`}
        data-testid={testId}
      >
        {value}
      </div>
    </div>
  );
}

function SectionHeader({
  kicker,
  title,
  compact,
}: {
  kicker: string;
  title: string;
  compact?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] text-primary uppercase tracking-widest">
        {kicker}
      </div>
      <h2
        className={`${compact ? "text-xl md:text-2xl" : "text-2xl md:text-3xl"} font-bold uppercase tracking-tight mt-2 max-w-2xl`}
      >
        {title}
      </h2>
    </div>
  );
}

function MethodCard({
  tag,
  title,
  body,
}: {
  tag: string;
  title: string;
  body: string;
}) {
  return (
    <div className="bg-background p-6">
      <div className="text-[10px] text-primary uppercase tracking-widest">
        {tag}
      </div>
      <div className="text-base font-bold uppercase tracking-tight mt-2">
        {title}
      </div>
      <p className="text-xs text-muted-foreground mt-3 leading-relaxed">
        {body}
      </p>
    </div>
  );
}

function Faq({ q, a }: { q: string; a: string }) {
  return (
    <div className="border-l-2 border-primary pl-4">
      <div className="text-sm font-bold uppercase tracking-tight">{q}</div>
      <p className="text-xs text-muted-foreground mt-2 leading-relaxed">{a}</p>
    </div>
  );
}
