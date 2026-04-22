import { Link } from "wouter";
import { useAuth, SignUpButton } from "@clerk/react";
import { PublicNav, PublicFooter } from "@/components/public/public-nav";

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-primary/30 bg-primary/5 px-4 py-1.5 text-xs font-medium text-foreground/90">
      {children}
    </span>
  );
}

function PrincipleCard({
  tag,
  title,
  body,
  testId,
}: {
  tag: string;
  title: string;
  body: string;
  testId: string;
}) {
  return (
    <div
      className="rounded-2xl border border-border bg-card/60 p-6 hover:border-primary/40 transition-colors"
      data-testid={testId}
    >
      <div className="text-[10px] uppercase tracking-[0.22em] text-primary mb-3">
        {tag}
      </div>
      <h3 className="text-lg font-semibold text-foreground mb-2">{title}</h3>
      <p className="text-sm text-muted-foreground leading-relaxed">{body}</p>
    </div>
  );
}

export default function AboutPage() {
  const { isSignedIn } = useAuth();

  return (
    <div className="min-h-[100dvh] bg-background text-foreground font-sans selection:bg-primary/30">
      <PublicNav active="about" />

      {/* ---------- Hero ---------- */}
      <section className="relative bg-starfield overflow-hidden">
        <div className="absolute inset-0 grid-bg opacity-40 pointer-events-none" />
        <div className="relative max-w-4xl mx-auto px-4 md:px-6 pt-20 md:pt-28 pb-16 md:pb-20 text-center">
          <Pill>
            <span className="text-primary mr-1.5">✦</span>
            About Axiom Predict
          </Pill>
          <h1
            className="mt-8 text-4xl md:text-6xl font-extrabold tracking-tight leading-[1.05]"
            data-testid="text-about-title"
          >
            We built the trader{" "}
            <span className="text-gradient-brand">we wanted to read.</span>
          </h1>
          <p className="mt-8 text-base md:text-lg text-muted-foreground max-w-2xl mx-auto leading-relaxed">
            Axiom Predict is a continuously running, fully-observed
            prediction-market trading desk. Every pick is debated by six analyst
            personas, gated by twelve risk checks, and recorded as a complete
            transcript you can replay. No black box, no curated highlights — just
            the live equity curve and the receipts.
          </p>
        </div>
      </section>

      {/* ---------- Mission ---------- */}
      <section className="border-t border-border/60">
        <div className="max-w-4xl mx-auto px-4 md:px-6 py-20 md:py-24">
          <div className="text-[11px] uppercase tracking-[0.22em] text-primary mb-4">
            Mission
          </div>
          <h2 className="text-2xl md:text-4xl font-extrabold tracking-tight mb-6">
            Make systematic prediction-market trading{" "}
            <span className="text-gradient-brand">legible.</span>
          </h2>
          <div className="space-y-5 text-base text-muted-foreground leading-relaxed">
            <p>
              Most quant strategies are sold as numbers in a PDF. Axiom Predict
              flips the model: the strategy <em className="text-foreground">is</em>{" "}
              the product, and the receipts are public. The same console our
              operators use to trade Kalshi is the console you sign into.
            </p>
            <p>
              We think the only honest way to evaluate a trading system is to
              watch it trade — in real money, against real markets, with the
              full transcript on tap. That's what this site is.
            </p>
          </div>
        </div>
      </section>

      {/* ---------- Methodology recap ---------- */}
      <section className="border-t border-border/60 bg-card/40">
        <div className="max-w-6xl mx-auto px-4 md:px-6 py-20 md:py-28">
          <div className="text-[11px] uppercase tracking-[0.22em] text-primary mb-3 text-center">
            How it works
          </div>
          <h2 className="text-2xl md:text-4xl font-extrabold tracking-tight text-center max-w-3xl mx-auto">
            Six personas. Twelve gates.{" "}
            <span className="text-gradient-brand">One audit log.</span>
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5 mt-12">
            <PrincipleCard
              tag="The swarm"
              title="Six analyst personas"
              body="Quantitative, contrarian, momentum, fundamentals, sentiment, and devil's advocate independently price every market and then debate. Per-persona calibration is tracked and weighted over time."
              testId="card-principle-swarm"
            />
            <PrincipleCard
              tag="The gates"
              title="Twelve-gate risk loop"
              body="Liquidity, spread, slippage, edge vs. fee, exposure caps, drawdown, kill-switch, persona disagreement bands, vertical limits — any single veto kills the order before it sizes."
              testId="card-principle-gates"
            />
            <PrincipleCard
              tag="The loop"
              title="Continuous research → execute"
              body="A scheduler runs cycles 24/7 against Kalshi. Every transcript — pick, debate, gate verdict, fill, settlement, post-mortem — is queryable inside the operator console."
              testId="card-principle-loop"
            />
          </div>
        </div>
      </section>

      {/* ---------- Responsible trading ---------- */}
      <section className="border-t border-border/60">
        <div className="max-w-4xl mx-auto px-4 md:px-6 py-20 md:py-24">
          <div className="text-[11px] uppercase tracking-[0.22em] text-warning mb-4">
            Responsible trading
          </div>
          <h2 className="text-2xl md:text-3xl font-extrabold tracking-tight mb-6">
            What this is — and what it{" "}
            <span className="text-gradient-brand">isn't.</span>
          </h2>
          <div className="space-y-4 text-sm md:text-base text-muted-foreground leading-relaxed">
            <p>
              <span className="text-foreground font-semibold">Axiom Predict is telemetry.</span>{" "}
              Subscribers get read access to the operator's live trading desk —
              signals, debate transcripts, fills, and post-mortems. We do not
              route orders for subscribers, manage their bankroll, or provide
              individualized financial advice.
            </p>
            <p>
              <span className="text-foreground font-semibold">Past performance is not predictive.</span>{" "}
              The live equity curve is real and unedited, but no trading system
              avoids drawdowns. Models drift, markets regime-shift, and the
              swarm can be wrong about a vertical for weeks at a time. Plan for
              it.
            </p>
            <p>
              <span className="text-foreground font-semibold">Trade only what you can afford to lose.</span>{" "}
              Prediction markets are leveraged in their own way — binary
              outcomes can move 100% against you in a single tick. If you
              decide to mirror any of the signals on your own Kalshi account,
              size accordingly and respect the gates.
            </p>
          </div>
        </div>
      </section>

      {/* ---------- CTA ---------- */}
      <section className="border-t border-border/60 bg-starfield">
        <div className="max-w-3xl mx-auto px-4 md:px-6 py-20 text-center">
          <Pill>
            <span className="text-primary mr-1.5">✦</span>
            Ready to look inside?
          </Pill>
          <h2 className="mt-8 text-3xl md:text-5xl font-extrabold tracking-tight leading-tight">
            Open the console.{" "}
            <span className="text-gradient-brand">Read the receipts.</span>
          </h2>
          <p className="text-base text-muted-foreground mt-6 max-w-xl mx-auto">
            Watch the live equity curve, browse settled signals, and see exactly
            why every trade fired.
          </p>
          <div className="mt-10 flex justify-center gap-3 flex-wrap">
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
            <Link
              href="/"
              className="rounded-full border border-border px-7 py-3.5 text-sm font-medium text-foreground hover:border-primary/60 hover:text-primary transition-colors"
              data-testid="link-cta-home"
            >
              See live performance
            </Link>
          </div>
        </div>
      </section>

      <PublicFooter />
    </div>
  );
}
