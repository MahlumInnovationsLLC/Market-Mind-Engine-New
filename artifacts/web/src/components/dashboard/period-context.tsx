import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export type PeriodKey = "1d" | "7d" | "30d" | "ytd";

const PERIOD_DAYS: Record<PeriodKey, number> = {
  "1d": 1,
  "7d": 7,
  "30d": 30,
  ytd: ytdDays(),
};

function ytdDays(): number {
  const now = new Date();
  const start = new Date(Date.UTC(now.getUTCFullYear(), 0, 1));
  const diffMs = now.getTime() - start.getTime();
  return Math.max(1, Math.ceil(diffMs / 86_400_000));
}

type Ctx = {
  period: PeriodKey;
  days: number;
  setPeriod: (p: PeriodKey) => void;
};

const PeriodContext = createContext<Ctx | null>(null);

export function PeriodProvider({ children }: { children: ReactNode }) {
  const [period, setPeriod] = useState<PeriodKey>("7d");
  const value = useMemo<Ctx>(
    () => ({ period, days: PERIOD_DAYS[period], setPeriod }),
    [period],
  );
  return <PeriodContext.Provider value={value}>{children}</PeriodContext.Provider>;
}

export function usePeriod(): Ctx {
  const ctx = useContext(PeriodContext);
  if (!ctx) {
    throw new Error("usePeriod must be used inside <PeriodProvider>");
  }
  return ctx;
}

export function PeriodStrip() {
  const { period, setPeriod } = usePeriod();
  const periods: { key: PeriodKey; label: string }[] = [
    { key: "1d", label: "1D" },
    { key: "7d", label: "7D" },
    { key: "30d", label: "30D" },
    { key: "ytd", label: "YTD" },
  ];
  return (
    <div
      className="inline-flex items-center gap-1 border border-border bg-muted/20 p-0.5"
      role="group"
      aria-label="Reporting period"
      data-testid="period-strip"
    >
      {periods.map((p) => {
        const active = period === p.key;
        return (
          <button
            key={p.key}
            type="button"
            onClick={() => setPeriod(p.key)}
            data-testid={`button-period-${p.key}`}
            aria-pressed={active}
            className={`px-3 py-1 text-[10px] uppercase tracking-widest font-mono transition-colors ${
              active
                ? "bg-primary/15 text-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {p.label}
          </button>
        );
      })}
    </div>
  );
}
