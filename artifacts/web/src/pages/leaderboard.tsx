import { useEffect, useState } from "react";
import { Link } from "wouter";
import { PublicNav, PublicFooter } from "@/components/public/public-nav";
import { apiFetch } from "@/lib/api";

interface PublicStats {
  realized_pnl: number | null;
  total_trades: number | null;
  win_rate: number | null;
  last_trade_at: string | null;
  trading_cycles: number | null;
  swarm_debates: number | null;
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

function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const formatted =
    abs >= 1000
      ? `$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
      : `$${abs.toFixed(2)}`;
  return n < 0 ? `-${formatted}` : formatted;
}

function formatPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function formatInt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return Math.round(n).toLocaleString();
}

function signalRoi(s: LiveSignal): number | null {
  // Roughly: (settle - entry) on the chosen side. Both are 0..1 prob units.
  if (
    s.entry_price === null ||
    s.entry_price === undefined ||
    s.settle_price === null ||
    s.settle_price === undefined
  ) {
    return null;
  }
  const dir = s.side && s.side.toLowerCase() === "no" ? -1 : 1;
  return (s.settle_price - s.entry_price) * dir;
}

function signalWon(s: LiveSignal): boolean | null {
  if (s.actual_outcome === null || s.actual_outcome === undefined) return null;
  // Convention: actual_outcome is 1 when the chosen side hit, 0 otherwise.
  return s.actual_outcome >= 0.5;
}

function StatCard({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}) {
  return (
    <div
      className="rounded-2xl border border-border bg-card/60 p-5"
      data-testid={testId}
    >
      <div className="text-[10px] uppercase tracking-[0.22em] text-muted-foreground mb-2">
        {label}
      </div>
      <div className="text-2xl md:text-3xl font-extrabold tabular-nums text-foreground">
        {value}
      </div>
    </div>
  );
}

export default function LeaderboardPage() {
  const [stats, setStats] = useState<PublicStats | null>(null);
  const [signals, setSignals] = useState<LiveSignal[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancel = false;
    apiFetch<PublicStats>("/api/public/stats")
      .then((data) => {
        if (!cancel) setStats(data);
      })
      .catch(() => {
        // Stats are best-effort.
      });
    apiFetch<RecentSignalsResponse>("/api/public/recent-signals?limit=12")
      .then((data) => {
        if (!cancel) setSignals(data.signals ?? []);
      })
      .catch((e) => {
        if (cancel) return;
        setSignals([]);
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancel = true;
    };
  }, []);

  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-sans selection:bg-primary/30">
      <PublicNav active="leaderboard" />

      {/* ---------- Header ---------- */}
      <section className="relative bg-starfield overflow-hidden">
        <div className="absolute inset-0 grid-bg opacity-40 pointer-events-none" />
        <div className="relative max-w-5xl mx-auto px-4 md:px-6 pt-20 md:pt-24 pb-12 text-center">
          <span className="inline-flex items-center rounded-full border border-primary/30 bg-primary/5 px-4 py-1.5 text-xs font-medium text-foreground/90">
            <span className="text-primary mr-1.5">✦</span>
            Public leaderboard
          </span>
          <h1
            className="mt-8 text-4xl md:text-6xl font-extrabold tracking-tight leading-[1.05]"
            data-testid="text-leaderboard-title"
          >
            Anonymized trader{" "}
            <span className="text-gradient-brand">activity.</span>
          </h1>
          <p className="mt-6 text-base md:text-lg text-muted-foreground max-w-2xl mx-auto leading-relaxed">
            The most-recent settled signals from the operator desk — anonymized
            to vertical, model probability, settle price, and outcome. No
            curation, no hand-picking.
          </p>
        </div>
      </section>

      {/* ---------- Stats strip ---------- */}
      <section className="border-t border-border/60">
        <div className="max-w-5xl mx-auto px-4 md:px-6 py-12 grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard
            label="Realized PnL"
            value={formatUsd(stats?.realized_pnl)}
            testId="stat-leaderboard-pnl"
          />
          <StatCard
            label="Win rate"
            value={formatPct(stats?.win_rate)}
            testId="stat-leaderboard-winrate"
          />
          <StatCard
            label="Trades"
            value={formatInt(stats?.total_trades)}
            testId="stat-leaderboard-trades"
          />
          <StatCard
            label="Swarm debates"
            value={formatInt(stats?.swarm_debates)}
            testId="stat-leaderboard-debates"
          />
        </div>
      </section>

      {/* ---------- Operator ranking — coming soon ---------- */}
      <section className="border-t border-border/60 bg-card/40">
        <div className="max-w-5xl mx-auto px-4 md:px-6 py-16 md:py-20">
          <div className="text-[11px] uppercase tracking-[0.22em] text-primary mb-3 text-center">
            Operator ranking
          </div>
          <h2 className="text-2xl md:text-3xl font-extrabold tracking-tight text-center">
            Top anonymized traders —{" "}
            <span className="text-gradient-brand">coming soon.</span>
          </h2>
          <div
            className="mt-10 rounded-2xl border border-dashed border-primary/40 bg-background/60 p-10 md:p-14 text-center"
            data-testid="card-leaderboard-coming-soon"
          >
            <div className="mx-auto mb-5 inline-flex items-center justify-center rounded-full border border-primary/40 bg-primary/10 text-primary w-12 h-12 text-xl">
              ✦
            </div>
            <h3 className="text-xl font-semibold text-foreground mb-3">
              Multi-operator leaderboard is coming online.
            </h3>
            <p className="text-sm text-muted-foreground max-w-xl mx-auto leading-relaxed">
              Today this site reports a single operator's live equity curve. As
              additional operators graduate from shadow trading, an anonymized
              ranking — ROI, win rate, swarm-debate volume — will appear here.
              Subscribe to the early-access list to be notified the moment it
              ships.
            </p>
            <div className="mt-7 flex justify-center gap-3 flex-wrap">
              <Link
                href="/#early-access"
                className="rounded-full border border-primary/60 text-primary px-5 py-2.5 text-sm font-semibold hover:bg-primary hover:text-primary-foreground transition-colors"
                data-testid="link-leaderboard-early-access"
              >
                Join early access →
              </Link>
              <Link
                href="/about"
                className="rounded-full border border-border px-5 py-2.5 text-sm font-medium text-muted-foreground hover:border-primary/60 hover:text-primary transition-colors"
                data-testid="link-leaderboard-about"
              >
                How it works
              </Link>
            </div>
          </div>
        </div>
      </section>

      {/* ---------- Recent settled signals ---------- */}
      <section className="border-t border-border/60">
        <div className="max-w-5xl mx-auto px-4 md:px-6 py-16 md:py-20">
          <div className="flex items-end justify-between gap-4 mb-8">
            <div>
              <div className="text-[11px] uppercase tracking-[0.22em] text-primary mb-2">
                Recent settled signals
              </div>
              <h2 className="text-2xl md:text-3xl font-extrabold tracking-tight">
                Last twelve from the desk
              </h2>
              <p className="text-sm text-muted-foreground mt-2 max-w-2xl">
                Anonymized to vertical, side, model probability, settle price,
                and outcome. Same data the live equity curve is built from — no
                cherry-picking.
              </p>
            </div>
          </div>

          {error && (
            <div
              className="rounded-xl border border-destructive/40 bg-destructive/10 text-destructive p-4 mb-6 text-sm"
              data-testid="text-leaderboard-error"
            >
              Live data temporarily unavailable: {error}
            </div>
          )}

          <div
            className="rounded-2xl border border-border bg-background/80 overflow-hidden"
            data-testid="table-leaderboard"
          >
            {signals === null ? (
              <div className="p-10 text-center text-sm text-muted-foreground">
                Loading…
              </div>
            ) : signals.length === 0 ? (
              <div
                className="p-10 text-center text-sm text-muted-foreground"
                data-testid="empty-leaderboard"
              >
                No settled signals yet — the next trading cycle will fill this
                feed.
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-card/80">
                  <tr className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                    <th className="text-left px-4 py-3 font-medium">#</th>
                    <th className="text-left px-4 py-3 font-medium">Vertical</th>
                    <th className="text-left px-4 py-3 font-medium">Side</th>
                    <th className="text-right px-4 py-3 font-medium">Model</th>
                    <th className="text-right px-4 py-3 font-medium">Settle</th>
                    <th className="text-right px-4 py-3 font-medium">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {signals.map((s, i) => {
                    const won = signalWon(s);
                    const roi = signalRoi(s);
                    return (
                      <tr
                        key={i}
                        className="border-t border-border/60 hover:bg-card/40 transition-colors"
                        data-testid={`row-leaderboard-${i}`}
                      >
                        <td className="px-4 py-3 text-muted-foreground tabular-nums">
                          {i + 1}
                        </td>
                        <td className="px-4 py-3 text-foreground/90">
                          <span className="capitalize">
                            {s.vertical || "—"}
                          </span>
                          {s.league && (
                            <span className="ml-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                              {s.league}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3 uppercase text-xs tracking-widest text-muted-foreground">
                          {s.side || "—"}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-foreground/90">
                          {s.model_prob !== null && s.model_prob !== undefined
                            ? `${(s.model_prob * 100).toFixed(1)}%`
                            : "—"}
                        </td>
                        <td className="px-4 py-3 text-right tabular-nums text-foreground/90">
                          {s.settle_price !== null && s.settle_price !== undefined
                            ? `${(s.settle_price * 100).toFixed(1)}%`
                            : "—"}
                        </td>
                        <td className="px-4 py-3 text-right">
                          {won === null ? (
                            <span className="text-muted-foreground">—</span>
                          ) : won ? (
                            <span
                              className="inline-flex items-center gap-1.5 rounded-full bg-success/10 text-success border border-success/30 px-2.5 py-0.5 text-xs font-semibold tabular-nums"
                              data-testid={`badge-leaderboard-win-${i}`}
                            >
                              ✓ {roi !== null ? formatPct(roi) : "win"}
                            </span>
                          ) : (
                            <span
                              className="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 text-destructive border border-destructive/30 px-2.5 py-0.5 text-xs font-semibold tabular-nums"
                              data-testid={`badge-leaderboard-loss-${i}`}
                            >
                              ✗ {roi !== null ? formatPct(roi) : "loss"}
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </section>

      {/* ---------- CTA ---------- */}
      <section className="border-t border-border/60">
        <div className="max-w-3xl mx-auto px-4 md:px-6 py-16 text-center">
          <h2 className="text-2xl md:text-3xl font-extrabold tracking-tight">
            Want the full transcripts?
          </h2>
          <p className="text-base text-muted-foreground mt-4 max-w-xl mx-auto">
            Pro and Elite subscribers see every persona's argument, gate verdict,
            and post-mortem behind each row above.
          </p>
          <div className="mt-8 flex justify-center gap-3 flex-wrap">
            <Link
              href="/pricing"
              className="rounded-full bg-primary text-primary-foreground px-7 py-3.5 text-sm font-semibold hover:opacity-90 transition-opacity glow-primary"
              data-testid="link-leaderboard-pricing"
            >
              See pricing →
            </Link>
            <Link
              href="/"
              className="rounded-full border border-border px-7 py-3.5 text-sm font-medium text-foreground hover:border-primary/60 hover:text-primary transition-colors"
              data-testid="link-leaderboard-home"
            >
              Live equity curve
            </Link>
          </div>
        </div>
      </section>

      <PublicFooter />
    </div>
  );
}
