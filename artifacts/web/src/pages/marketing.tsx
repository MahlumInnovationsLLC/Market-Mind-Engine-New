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

interface LiveSignal {
  vertical: string | null;
  league: string | null;
  side: string | null;
  model_prob: number | null;
  entry_price: number | null;
  actual_outcome: number | null;
  settle_price: number | null;
  settled_at: string | null;
}

interface RecentSignalsResponse {
  signals: LiveSignal[];
}

const INTEREST_OPTIONS: { value: string; label: string }[] = [
  { value: "politics", label: "Politics" },
  { value: "sports", label: "Sports" },
  { value: "macro", label: "Macro / Fed" },
  { value: "crypto", label: "Crypto" },
  { value: "weather", label: "Weather" },
  { value: "other", label: "Other" },
];

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

function BrandMark({ size = 32 }: { size?: number }) {
  // Simple chevron / mountain-peak mark inspired by the reference site
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="brand-mark-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="hsl(189 94% 60%)" />
          <stop offset="100%" stopColor="hsl(258 90% 70%)" />
        </linearGradient>
      </defs>
      <path
        d="M16 4 L28 26 H22 L16 14 L10 26 H4 Z"
        stroke="url(#brand-mark-grad)"
        strokeWidth="1.75"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

export default function MarketingPage() {
  const { isSignedIn } = useAuth();
  const [range, setRange] = useState<RangeKey>("30");
  const [curve, setCurve] = useState<EquityPoint[]>([]);
  const [curveLoading, setCurveLoading] = useState(true);
  const [curveError, setCurveError] = useState<string | null>(null);
  const [stats, setStats] = useState<PublicStats | null>(null);
  const [catalog, setCatalog] = useState<PricingCatalog | null>(null);
  const [signals, setSignals] = useState<LiveSignal[] | null>(null);
  const [signalsError, setSignalsError] = useState<string | null>(null);
  const [leadEmail, setLeadEmail] = useState("");
  const [leadInterest, setLeadInterest] = useState<string>(INTEREST_OPTIONS[0].value);
  const [leadSubmitting, setLeadSubmitting] = useState(false);
  const [leadStatus, setLeadStatus] = useState<
    | { kind: "ok"; message: string }
    | { kind: "error"; message: string }
    | null
  >(null);

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
    apiFetch<RecentSignalsResponse>("/api/public/recent-signals?limit=4")
      .then((data) => {
        if (!cancel) setSignals(data.signals ?? []);
      })
      .catch((e) => {
        if (!cancel) {
          setSignals([]);
          setSignalsError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancel = true;
    };
  }, []);

  async function submitLead(ev: React.FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    if (leadSubmitting) return;
    const email = leadEmail.trim();
    if (!email || !email.includes("@")) {
      setLeadStatus({ kind: "error", message: "Please enter a valid email." });
      return;
    }
    setLeadSubmitting(true);
    setLeadStatus(null);
    try {
      await apiFetch("/api/public/leads", {
        method: "POST",
        body: { email, interest: leadInterest, source: "landing" },
      });
      setLeadStatus({
        kind: "ok",
        message:
          "You're on the list. Check your inbox for a confirmation from Axiom Predict.",
      });
      setLeadEmail("");
    } catch (e) {
      const msg =
        e instanceof Error
          ? e.message
          : "Couldn't submit right now. Please try again.";
      setLeadStatus({ kind: "error", message: msg });
    } finally {
      setLeadSubmitting(false);
    }
  }

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
    <div className="min-h-[100dvh] bg-background text-foreground font-sans selection:bg-primary/30">
      {/* ---------- Top Nav ---------- */}
      <nav className="sticky top-0 z-30 backdrop-blur-md bg-background/70 border-b border-border/60">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-3 flex items-center justify-between">
          <Link
            href="/"
            className="flex items-center gap-2.5"
            data-testid="link-home-logo"
          >
            <BrandMark size={28} />
            <div className="flex flex-col leading-none">
              <span className="text-sm font-bold tracking-wide text-foreground">
                AXIOM
              </span>
              <span className="text-[9px] tracking-[0.25em] text-muted-foreground -mt-0.5">
                PREDICT
              </span>
            </div>
          </Link>
          <div className="flex items-center gap-1 md:gap-2 text-sm">
            <a
              href="#methodology"
              className="hidden md:inline px-3 py-2 text-muted-foreground hover:text-foreground transition-colors"
            >
              Methodology
            </a>
            <a
              href="#equity"
              className="hidden md:inline px-3 py-2 text-muted-foreground hover:text-foreground transition-colors"
            >
              Live Performance
            </a>
            <a
              href="#signals"
              className="hidden md:inline px-3 py-2 text-muted-foreground hover:text-foreground transition-colors"
            >
              Signals
            </a>
            <a
              href="#pricing"
              className="hidden md:inline px-3 py-2 text-muted-foreground hover:text-foreground transition-colors"
            >
              Pricing
            </a>
            <a
              href="#mobile"
              className="hidden md:inline text-muted-foreground hover:text-primary uppercase tracking-widest"
            >
              Mobile
            </a>
            {isSignedIn ? (
              <Link
                href="/app"
                className="ml-2 inline-flex items-center gap-1.5 rounded-full border border-primary/60 text-primary px-4 py-1.5 text-sm font-medium hover:bg-primary hover:text-primary-foreground transition-colors"
                data-testid="link-open-console"
              >
                Open Console
                <span aria-hidden>→</span>
              </Link>
            ) : (
              <>
                <SignInButton mode="modal">
                  <button
                    type="button"
                    className="px-3 py-2 text-muted-foreground hover:text-foreground transition-colors"
                    data-testid="button-nav-signin"
                  >
                    Sign in
                  </button>
                </SignInButton>
                <SignUpButton mode="modal">
                  <button
                    type="button"
                    className="ml-2 inline-flex items-center gap-1.5 rounded-full border border-primary/60 text-primary px-4 py-1.5 text-sm font-medium hover:bg-primary hover:text-primary-foreground transition-colors"
                    data-testid="button-nav-signup"
                  >
                    Get Access
                    <span aria-hidden>→</span>
                  </button>
                </SignUpButton>
              </>
            )}
          </div>
        </div>
      </nav>

      {/* ---------- Hero ---------- */}
      <section className="relative bg-starfield overflow-hidden">
        <div className="max-w-5xl mx-auto px-4 md:px-6 pt-24 md:pt-32 pb-20 md:pb-28 text-center">
          <Pill>
            <span className="text-primary mr-1.5">✦</span>
            Live prediction-market trading · Kalshi
          </Pill>
          <h1 className="mt-8 text-4xl md:text-6xl lg:text-7xl font-extrabold tracking-tight leading-[1.05]">
            <span className="text-gradient-brand">Six analyst LLMs.</span>
            <br />
            One twelve-gate
            <br />
            risk loop.
          </h1>
          <p className="mt-8 text-base md:text-lg text-muted-foreground max-w-2xl mx-auto leading-relaxed">
            Axiom-Predict runs a continuous{" "}
            <span className="text-primary">research-debate-execute</span> loop on
            Kalshi. Six personas argue every pick, twelve risk gates decide what
            gets sized, and the entire transcript — including this{" "}
            <span className="text-primary">live equity curve</span> — is yours
            to inspect.
          </p>
          <div className="mt-10 flex flex-wrap justify-center gap-3">
            {isSignedIn ? (
              <Link
                href="/app"
                className="rounded-full bg-primary text-primary-foreground px-7 py-3.5 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary"
                data-testid="link-hero-console"
              >
                Open operator console →
              </Link>
            ) : (
              <SignUpButton mode="modal">
                <button
                  type="button"
                  className="rounded-full bg-primary text-primary-foreground px-7 py-3.5 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary"
                  data-testid="button-hero-signup"
                >
                  Get access →
                </button>
              </SignUpButton>
            )}
            <a
              href="#equity"
              className="rounded-full border border-border px-7 py-3.5 text-sm font-medium text-foreground hover:border-primary/60 hover:text-primary transition-colors"
            >
              See live performance
            </a>
          </div>

          {/* Hero stat strip */}
          <div className="mt-16 grid grid-cols-2 md:grid-cols-4 gap-3 max-w-4xl mx-auto">
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

          <div className="mt-10 flex flex-wrap justify-center gap-x-6 gap-y-2 text-xs text-muted-foreground">
            <span>Six-persona swarm</span>
            <span className="text-primary">·</span>
            <span>Twelve risk gates</span>
            <span className="text-primary">·</span>
            <span>Live Kalshi fills</span>
            <span className="text-primary">·</span>
            <span>Full transcript audit</span>
          </div>
        </div>
      </section>

      {/* ---------- Methodology ---------- */}
      <section id="methodology" className="border-t border-border/60">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-20 md:py-28">
          <SectionHeader
            kicker="Methodology"
            title={
              <>
                No black box. Every pick is{" "}
                <span className="text-gradient-brand">debated, gated,</span> and
                logged.
              </>
            }
            subtitle="The full pipeline — from raw market scrape to settled fill — is observable, queryable, and explainable. Nothing happens that you can't replay."
          />
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5 mt-14">
            <MethodCard
              tag="The swarm"
              title="Six-persona debate"
              body="Six analyst personas — quantitative, contrarian, momentum, fundamentals, sentiment, and a designated devil's advocate — independently price every market and then argue. Calibration metrics for each persona are tracked and weighted over time."
            />
            <MethodCard
              tag="The gates"
              title="Twelve-gate risk control"
              body="Before any size hits the book a candidate trade clears twelve sequential gates: liquidity, spread, slippage, edge vs. fee, exposure caps, drawdown, kill-switch, persona disagreement bands, and more. A single veto kills the order."
            />
            <MethodCard
              tag="The loop"
              title="Continuous research → execute"
              body="A scheduler runs trading cycles 24/7 on Kalshi. Every cycle's full transcript — pick, debate, gate verdicts, fill, settlement, post-mortem — is persisted and queryable in your console."
            />
          </div>
        </div>
      </section>

      {/* ---------- Live Equity Curve ---------- */}
      <section
        id="equity"
        className="relative border-t border-border/60 bg-card/40"
      >
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-20 md:py-28">
          <div className="flex flex-col md:flex-row md:items-end justify-between gap-6 mb-10">
            <SectionHeader
              kicker="Live performance"
              title={
                <>
                  Real fills. <span className="text-gradient-brand">Real PnL.</span>{" "}
                  Updated every cycle.
                </>
              }
              compact
            />
            <div
              className="inline-flex rounded-full border border-border bg-background/60 p-1 self-start md:self-auto"
              role="group"
            >
              {(["7", "30", "90"] as RangeKey[]).map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRange(r)}
                  className={`px-4 py-1.5 text-xs font-semibold rounded-full transition-colors ${
                    range === r
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                  data-testid={`button-range-${r}`}
                >
                  {RANGE_LABEL[r]}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6">
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
            className="rounded-2xl border border-border bg-background/80 h-[360px] p-3"
            data-testid="chart-equity-curve"
          >
            {curveLoading ? (
              <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
                Loading…
              </div>
            ) : curveError ? (
              <div className="h-full flex items-center justify-center text-sm text-destructive p-4 text-center">
                Equity curve unavailable: {curveError}
              </div>
            ) : curve.length === 0 ? (
              <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
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
                        stopColor="hsl(189 94% 55%)"
                        stopOpacity={0.45}
                      />
                      <stop
                        offset="100%"
                        stopColor="hsl(189 94% 55%)"
                        stopOpacity={0}
                      />
                    </linearGradient>
                  </defs>
                  <CartesianGrid
                    strokeDasharray="2 4"
                    stroke="hsl(220 25% 18%)"
                  />
                  <XAxis
                    dataKey="date"
                    stroke="hsl(215 20% 70%)"
                    tick={{ fontSize: 11, fontFamily: "Inter, sans-serif" }}
                    tickFormatter={(v: string) => v.slice(5)}
                    minTickGap={24}
                  />
                  <YAxis
                    stroke="hsl(215 20% 70%)"
                    tick={{ fontSize: 11, fontFamily: "Inter, sans-serif" }}
                    tickFormatter={(v: number) => formatUsd(v)}
                    width={70}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "hsl(222 35% 7%)",
                      border: "1px solid hsl(220 25% 18%)",
                      borderRadius: 12,
                      fontSize: 12,
                      fontFamily: "Inter, sans-serif",
                    }}
                    labelStyle={{ color: "hsl(210 40% 98%)" }}
                    formatter={(value: number, name: string) => [
                      formatUsd(value, name === "daily_pnl"),
                      name === "cumulative_pnl" ? "Cumulative" : "Daily",
                    ]}
                  />
                  <Area
                    type="monotone"
                    dataKey="cumulative_pnl"
                    stroke="hsl(189 94% 55%)"
                    strokeWidth={2}
                    fill="url(#equityFill)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-4">
            Source: live <code className="text-primary font-mono">trades</code>{" "}
            table — refreshed every trading cycle.
          </p>
        </div>
      </section>

      {/* ---------- Live Signals Strip ---------- */}
      <section id="signals" className="border-t border-border/60">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-20 md:py-24">
          <SectionHeader
            kicker="Live signals"
            title={
              <>
                The four most-recent{" "}
                <span className="text-gradient-brand">settled signals.</span>
              </>
            }
            subtitle="Pulled live from the trades table — anonymized to vertical, model probability, market settle, and outcome. No cherry-picking, no curation."
          />
          <div
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-12"
            data-testid="grid-live-signals"
          >
            {signals === null ? (
              [0, 1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="rounded-2xl border border-border bg-card/60 h-44 animate-pulse"
                />
              ))
            ) : signals.length === 0 ? (
              <div
                className="col-span-full rounded-2xl border border-border bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground"
                data-testid="empty-live-signals"
              >
                {signalsError
                  ? "Live signals are temporarily unavailable. Check back shortly."
                  : "No settled signals yet — the next trading cycle will fill this strip."}
              </div>
            ) : (
              signals.map((s, i) => (
                <SignalCard key={i} signal={s} index={i} />
              ))
            )}
          </div>
        </div>
      </section>

      {/* ---------- Lead Capture ---------- */}
      <section id="early-access" className="border-t border-border/60 bg-card/40">
        <div className="max-w-3xl mx-auto px-4 md:px-6 py-20 md:py-24">
          <SectionHeader
            kicker="Early access"
            title={
              <>
                Want the next vertical?{" "}
                <span className="text-gradient-brand">Tell us where to look.</span>
              </>
            }
            subtitle="We're rolling new verticals out one at a time. Drop your email and the area you care about — we'll ping you the day it goes live."
          />
          <form
            onSubmit={submitLead}
            className="mt-10 rounded-2xl border border-border bg-background/80 p-6 md:p-8 space-y-5"
            data-testid="form-lead-capture"
            noValidate
          >
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <label className="flex flex-col gap-2">
                <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground font-medium">
                  Email
                </span>
                <input
                  type="email"
                  required
                  autoComplete="email"
                  placeholder="you@firm.com"
                  value={leadEmail}
                  onChange={(e) => setLeadEmail(e.target.value)}
                  className="rounded-xl border border-border bg-background px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:border-primary/60"
                  data-testid="input-lead-email"
                />
              </label>
              <label className="flex flex-col gap-2">
                <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground font-medium">
                  Interest area
                </span>
                <select
                  value={leadInterest}
                  onChange={(e) => setLeadInterest(e.target.value)}
                  className="rounded-xl border border-border bg-background px-4 py-3 text-sm text-foreground focus:outline-none focus:border-primary/60"
                  data-testid="select-lead-interest"
                >
                  {INTEREST_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <p className="text-xs text-muted-foreground">
                We'll only email you about Axiom Predict launches. No spam, unsubscribe anytime.
              </p>
              <button
                type="submit"
                disabled={leadSubmitting}
                className="rounded-full bg-primary text-primary-foreground px-6 py-3 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary disabled:opacity-60 disabled:cursor-not-allowed"
                data-testid="button-lead-submit"
              >
                {leadSubmitting ? "Submitting…" : "Notify me →"}
              </button>
            </div>
            {leadStatus && (
              <div
                className={`rounded-xl border px-4 py-3 text-sm ${
                  leadStatus.kind === "ok"
                    ? "border-success/40 bg-success/10 text-success"
                    : "border-destructive/40 bg-destructive/10 text-destructive"
                }`}
                data-testid={`lead-status-${leadStatus.kind}`}
                role="status"
              >
                {leadStatus.message}
              </div>
            )}
          </form>
        </div>
      </section>

      {/* ---------- Pricing ---------- */}
      <section id="pricing" className="border-t border-border/60">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-20 md:py-28">
          <SectionHeader
            kicker="Pricing"
            title={
              <>
                Pick a tier.{" "}
                <span className="text-gradient-brand">Cancel anytime.</span>
              </>
            }
            subtitle="Both tiers include the live equity curve, full methodology, and the Kalshi fill stream. Elite adds raw transcripts and per-persona scoring."
          />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5 mt-14 max-w-4xl mx-auto">
            {paidTiers.length === 0
              ? [0, 1].map((i) => (
                  <div
                    key={i}
                    className="rounded-2xl border border-border bg-card p-6 h-72 animate-pulse"
                  />
                ))
              : paidTiers.map((tier) => (
                  <TierCard key={tier.id} tier={tier} isSignedIn={!!isSignedIn} />
                ))}
          </div>
          <div className="mt-8 text-center">
            <Link
              href="/pricing"
              className="text-sm text-muted-foreground hover:text-primary transition-colors"
              data-testid="link-full-pricing"
            >
              Compare all tiers →
            </Link>
          </div>
        </div>
      </section>

      {/* ---------- Mobile ---------- */}
      <section id="mobile" className="border-b border-border">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-16 grid grid-cols-1 md:grid-cols-2 gap-10 items-center">
          <div>
            <SectionHeader
              kicker="Mobile"
              title="Take the swarm with you."
              compact
            />
            <p className="text-sm text-muted-foreground mt-4 max-w-md">
              The Axiom_Predict mobile app pushes live signals, settlement
              alerts, and post-mortems straight to your phone. iOS and Android
              builds are rolling out — tap a badge to be notified.
            </p>
            <div className="flex flex-wrap gap-3 mt-6">
              <a
                href="https://apps.apple.com/"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Download Axiom_Predict on the App Store"
                className="flex items-center gap-3 border border-border bg-card px-4 py-3 hover:border-primary"
                data-testid="link-app-store"
              >
                <span aria-hidden="true" className="text-primary text-lg leading-none"></span>
                <span className="flex flex-col leading-tight">
                  <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                    Download on the
                  </span>
                  <span className="text-sm font-bold uppercase tracking-tight">
                    App Store
                  </span>
                </span>
              </a>
              <a
                href="https://play.google.com/"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Get Axiom_Predict on Google Play"
                className="flex items-center gap-3 border border-border bg-card px-4 py-3 hover:border-primary"
                data-testid="link-play-store"
              >
                <span aria-hidden="true" className="text-primary text-lg leading-none">▶</span>
                <span className="flex flex-col leading-tight">
                  <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                    Get it on
                  </span>
                  <span className="text-sm font-bold uppercase tracking-tight">
                    Google Play
                  </span>
                </span>
              </a>
            </div>
            <p className="text-[10px] text-muted-foreground uppercase tracking-widest mt-4">
              Store listings pending review — links are placeholders.
            </p>
          </div>
          <div className="border border-border bg-card p-6 font-mono text-xs space-y-2">
            <div className="text-[10px] text-primary uppercase tracking-widest">
              ::push_preview
            </div>
            <div className="border-l-2 border-primary pl-3">
              <div className="text-foreground font-bold">
                AXIOM :: live signal
              </div>
              <div className="text-muted-foreground">
                KXPRES-2028-DEM @ 0.42 → +6.1% edge, gates 12/12 cleared.
              </div>
            </div>
            <div className="border-l-2 border-warning pl-3">
              <div className="text-foreground font-bold">
                AXIOM :: settlement
              </div>
              <div className="text-muted-foreground">
                FED-CUT-MAR settled YES — +$184.20 realized.
              </div>
            </div>
            <div className="border-l-2 border-destructive pl-3">
              <div className="text-foreground font-bold">
                AXIOM :: drift alert
              </div>
              <div className="text-muted-foreground">
                Politics vertical Brier drift &gt; 0.08 — review queue.
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ---------- FAQ ---------- */}
      <section className="border-t border-border/60 bg-card/40">
        <div className="max-w-5xl mx-auto px-4 md:px-6 py-20 md:py-28">
          <SectionHeader
            kicker="FAQ"
            title={<>Common questions</>}
            subtitle="Honest answers about what this is — and what it isn't."
          />
          <div className="mt-12 grid grid-cols-1 md:grid-cols-2 gap-6">
            <Faq
              q="Is this paper trading or live?"
              a="Live — orders are placed against Kalshi in real money mode. The equity curve above is real fills. A paper mode exists for backtesting but is not what's plotted here."
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
      <section className="border-t border-border/60 bg-starfield">
        <div className="max-w-3xl mx-auto px-4 md:px-6 py-24 text-center">
          <Pill>
            <span className="text-primary mr-1.5">✦</span>
            Entry point
          </Pill>
          <h2 className="mt-8 text-3xl md:text-5xl font-extrabold tracking-tight leading-tight">
            Watch a real swarm{" "}
            <span className="text-gradient-brand">trade real markets.</span>
          </h2>
          <p className="text-base text-muted-foreground mt-6 max-w-xl mx-auto">
            Create an account in under a minute. Pick a tier when you're ready.
          </p>
          <div className="mt-10 flex justify-center gap-3">
            {isSignedIn ? (
              <Link
                href="/app"
                className="rounded-full bg-primary text-primary-foreground px-7 py-3.5 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary"
                data-testid="link-cta-console"
              >
                Open console →
              </Link>
            ) : (
              <SignUpButton mode="modal">
                <button
                  type="button"
                  className="rounded-full bg-primary text-primary-foreground px-7 py-3.5 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary"
                  data-testid="button-cta-signup"
                >
                  Create account →
                </button>
              </SignUpButton>
            )}
          </div>
        </div>
      </section>

      {/* ---------- Footer ---------- */}
      <footer className="border-t border-border/60 py-10">
        <div className="max-w-6xl mx-auto px-4 md:px-6 flex flex-col md:flex-row items-center justify-between gap-4 text-sm text-muted-foreground">
          <div className="flex items-center gap-2">
            <BrandMark size={20} />
            <span>
              © Axiom Predict — operator-grade prediction-market trading
            </span>
          </div>
          <div className="flex gap-5">
            <Link
              href="/pricing"
              className="hover:text-primary transition-colors"
              data-testid="link-footer-pricing"
            >
              Pricing
            </Link>
            <Link
              href="/health"
              className="hover:text-primary transition-colors"
              data-testid="link-footer-health"
            >
              Status
            </Link>
            {isSignedIn ? (
              <Link
                href="/app"
                className="hover:text-primary transition-colors"
                data-testid="link-footer-app"
              >
                Console
              </Link>
            ) : (
              <SignInButton mode="modal">
                <button
                  type="button"
                  className="hover:text-primary transition-colors"
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

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-primary/30 bg-primary/5 px-4 py-1.5 text-xs font-medium text-foreground/90">
      {children}
    </span>
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

  const cls = `w-full inline-flex items-center justify-center rounded-full px-5 py-3 text-sm font-semibold transition-all ${
    tier.highlight
      ? "bg-primary text-primary-foreground hover:opacity-90 glow-primary"
      : "border border-border text-foreground hover:border-primary/60 hover:text-primary"
  }`;

  return (
    <div
      className={`relative rounded-2xl border bg-card p-7 flex flex-col ${
        tier.highlight
          ? "border-primary/50 shadow-[0_0_60px_-15px_hsl(189_94%_55%_/_0.4)]"
          : "border-border"
      }`}
      data-testid={`card-marketing-tier-${tier.id}`}
    >
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-lg font-semibold">{tier.name}</h3>
        {tier.highlight && (
          <span className="text-[10px] uppercase tracking-widest font-semibold rounded-full bg-primary/10 text-primary border border-primary/30 px-2.5 py-1">
            Popular
          </span>
        )}
      </div>
      <p className="text-sm text-muted-foreground min-h-[40px]">{tier.tagline}</p>
      <div className="mt-5 flex items-baseline gap-1.5">
        <span className="text-4xl font-extrabold text-foreground">
          ${tier.monthly_usd}
        </span>
        <span className="text-sm text-muted-foreground">/ month</span>
      </div>
      <ul className="text-sm text-foreground/80 space-y-2.5 my-6 flex-1">
        {tier.features.slice(0, 5).map((f) => (
          <li key={f} className="flex gap-2.5">
            <span className="text-primary mt-0.5" aria-hidden>
              ✓
            </span>
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
  testId: string;
  accent?: "positive" | "negative";
}) {
  const accentClass =
    accent === "positive"
      ? "text-success"
      : accent === "negative"
        ? "text-destructive"
        : "text-foreground";
  return (
    <div
      className="rounded-2xl border border-border bg-card/60 px-5 py-4 text-left"
      data-testid={testId}
    >
      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground font-medium">
        {label}
      </div>
      <div className={`mt-1.5 text-2xl md:text-3xl font-bold ${accentClass}`}>
        {value}
      </div>
    </div>
  );
}

function SectionHeader({
  kicker,
  title,
  subtitle,
  compact,
}: {
  kicker: string;
  title: React.ReactNode;
  subtitle?: string;
  compact?: boolean;
}) {
  return (
    <div className={compact ? "" : "max-w-3xl"}>
      <div className="text-xs font-semibold uppercase tracking-[0.2em] text-primary mb-3">
        {kicker}
      </div>
      <h2
        className={`font-extrabold tracking-tight leading-[1.1] ${
          compact ? "text-2xl md:text-3xl" : "text-3xl md:text-5xl"
        }`}
      >
        {title}
      </h2>
      {subtitle && (
        <p className="text-base text-muted-foreground mt-5 max-w-2xl">
          {subtitle}
        </p>
      )}
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
    <div className="group rounded-2xl border border-border bg-card p-7 hover:border-primary/40 transition-colors">
      <div className="text-[11px] font-semibold uppercase tracking-[0.2em] text-primary mb-4">
        {tag}
      </div>
      <h3 className="text-xl font-semibold mb-3">{title}</h3>
      <p className="text-sm text-muted-foreground leading-relaxed">{body}</p>
    </div>
  );
}

function SignalCard({ signal, index }: { signal: LiveSignal; index: number }) {
  const sideUpper = (signal.side || "").toUpperCase();
  const isYesSide = sideUpper === "YES" || sideUpper === "Y" || sideUpper === "BUY";
  const predictedYes = isYesSide
    ? signal.model_prob
    : signal.model_prob !== null
      ? 1 - signal.model_prob
      : null;
  const calledYes = isYesSide;
  const outcomeYes = signal.actual_outcome === 1;
  const correct =
    signal.actual_outcome === null ? null : calledYes === outcomeYes;
  const verticalLabel = (signal.vertical || "Market").replace(/_/g, " ");
  const leagueLabel = signal.league ? signal.league.toUpperCase() : null;
  const settledTs = signal.settled_at ? new Date(signal.settled_at) : null;
  const settledRel = settledTs ? relTime(settledTs) : "—";
  const settlePct =
    signal.settle_price !== null && signal.settle_price !== undefined
      ? `${(signal.settle_price * 100).toFixed(0)}¢`
      : "—";
  const modelPct =
    predictedYes !== null && predictedYes !== undefined
      ? `${(predictedYes * 100).toFixed(0)}%`
      : "—";

  return (
    <div
      className="rounded-2xl border border-border bg-card/70 p-5 flex flex-col gap-3 hover:border-primary/40 transition-colors"
      data-testid={`card-live-signal-${index}`}
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-primary">
          {verticalLabel}
          {leagueLabel ? ` · ${leagueLabel}` : ""}
        </span>
        {correct === null ? (
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Pending
          </span>
        ) : correct ? (
          <span className="text-[10px] uppercase tracking-widest font-semibold text-success">
            Hit
          </span>
        ) : (
          <span className="text-[10px] uppercase tracking-widest font-semibold text-destructive">
            Miss
          </span>
        )}
      </div>
      <div className="text-sm text-foreground">
        Swarm called <span className="font-semibold">{calledYes ? "YES" : "NO"}</span>{" "}
        with confidence <span className="font-semibold text-primary">{modelPct}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="rounded-lg border border-border bg-background/60 px-3 py-2">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Market settle
          </div>
          <div className="mt-1 font-mono text-sm text-foreground">{settlePct}</div>
        </div>
        <div className="rounded-lg border border-border bg-background/60 px-3 py-2">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Outcome
          </div>
          <div
            className={`mt-1 font-mono text-sm ${
              outcomeYes ? "text-success" : "text-destructive"
            }`}
          >
            {signal.actual_outcome === null
              ? "—"
              : outcomeYes
                ? "YES"
                : "NO"}
          </div>
        </div>
      </div>
      <div className="text-[11px] text-muted-foreground mt-auto">
        Settled {settledRel}
      </div>
    </div>
  );
}

function relTime(d: Date): string {
  const diffMs = Date.now() - d.getTime();
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

function Faq({ q, a }: { q: string; a: string }) {
  return (
    <div className="rounded-2xl border border-border bg-background/40 p-6">
      <div className="text-base font-semibold text-foreground mb-2">{q}</div>
      <p className="text-sm text-muted-foreground leading-relaxed">{a}</p>
    </div>
  );
}
