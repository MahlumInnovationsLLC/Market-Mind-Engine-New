import type { CryptoOhlcvRetentionStatus } from "@workspace/api-client-react";
import {
  CryptoOhlcvRetentionStatusHealth,
  getGetCryptoOhlcvRetentionStatusQueryKey,
  useGetCryptoOhlcvRetentionStatus,
} from "@workspace/api-client-react";
import { Database } from "lucide-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtUnix(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${seconds.toFixed(0)}s ago`;
  const m = seconds / 60;
  if (m < 90) return `${m.toFixed(0)}m ago`;
  const h = m / 60;
  if (h < 48) return `${h.toFixed(1)}h ago`;
  return `${(h / 24).toFixed(1)}d ago`;
}

function fmtInterval(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  if (seconds < 90) return `${seconds.toFixed(0)}s`;
  const m = seconds / 60;
  if (m < 90) return `${m.toFixed(0)}m`;
  const h = m / 60;
  return `${h.toFixed(1)}h`;
}

function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString();
}

const HEALTH_PILL: Record<
  CryptoOhlcvRetentionStatusHealth,
  { label: string; pill: string; banner: string | null; help: string }
> = {
  ok: {
    label: "OK",
    pill:
      "border border-success/60 text-success bg-success/10",
    banner: null,
    help: "Sweep is running and ticked within the expected window.",
  },
  stale: {
    label: "STALE",
    pill: "border border-warn/60 text-warn bg-warn/10",
    banner:
      "Last sweep is overdue — the loop should have ticked at least once within the last two intervals.",
    help: "The retention loop is meant to be running but hasn't ticked recently.",
  },
  error: {
    label: "ERROR",
    pill: "border border-loss/60 text-loss bg-loss/10",
    banner:
      "Most recent sweep raised an error. The loop will retry on its next tick — investigate the message below.",
    help: "Most recent sweep failed; the loop will retry on the next tick.",
  },
  disabled: {
    label: "DISABLED",
    pill:
      "border border-border text-muted-foreground bg-muted/10",
    banner:
      "Retention is intentionally off (AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED=0). The crypto_ohlcv table is not being pruned.",
    help: "Loop is disabled by env var; the table is not being pruned.",
  },
};

export default function CryptoOhlcvRetentionPanel() {
  const { data, isLoading, isError } = useGetCryptoOhlcvRetentionStatus({
    query: {
      queryKey: getGetCryptoOhlcvRetentionStatusQueryKey(),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-crypto-ohlcv-retention"
    >
      <SectionHeader
        eyebrow="STORAGE"
        title="Crypto bars retention"
        subtitle="Per-granularity TTL sweep that prunes the crypto_ohlcv table."
      />

      {isLoading ? (
        <div
          className="mt-3 h-32 animate-pulse border border-border bg-muted/10"
          data-testid="crypto-ohlcv-retention-loading"
        />
      ) : isError || !data ? (
        <div
          className="mt-3 border border-loss/60 bg-loss/10 p-3 text-xs text-loss"
          data-testid="crypto-ohlcv-retention-error"
        >
          ERR_RETENTION_FETCH_FAIL
        </div>
      ) : (
        <CryptoOhlcvRetentionBody data={data} />
      )}
    </GlassCard>
  );
}

function CryptoOhlcvRetentionBody({
  data,
}: {
  data: CryptoOhlcvRetentionStatus;
}) {
  const tone = HEALTH_PILL[data.health];
  const grans = Object.keys(data.retention_days).sort();
  const lastByGran = data.last_deleted_by_granularity ?? {};

  return (
    <div className="mt-3 space-y-3 text-xs" data-testid="crypto-ohlcv-retention-body">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`px-2 py-0.5 text-[10px] uppercase tracking-widest ${tone.pill}`}
          data-testid="crypto-ohlcv-retention-health"
          title={tone.help}
        >
          {tone.label}
        </span>
        <span
          className={`px-2 py-0.5 text-[10px] uppercase tracking-widest border ${
            data.running
              ? "border-success/40 text-success"
              : "border-border text-muted-foreground"
          }`}
        >
          {data.running ? "RUNNING" : "STOPPED"}
        </span>
        {!data.enabled ? (
          <span className="px-2 py-0.5 text-[10px] uppercase tracking-widest border border-border text-muted-foreground">
            DISABLED VIA ENV
          </span>
        ) : null}
        <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Database className="h-3 w-3" aria-hidden />
          crypto_ohlcv
        </span>
      </div>

      {tone.banner ? (
        <div
          className={`border p-2 text-[11px] ${
            data.health === "error"
              ? "border-loss/60 bg-loss/10 text-loss"
              : data.health === "stale"
                ? "border-warn/60 bg-warn/10 text-warn"
                : "border-border bg-muted/20 text-muted-foreground"
          }`}
          data-testid="crypto-ohlcv-retention-banner"
        >
          {tone.banner}
          {data.health === "error" && data.last_error ? (
            <pre
              className="mt-1 whitespace-pre-wrap break-words font-mono text-[10px] text-loss/90"
              data-testid="crypto-ohlcv-retention-error-message"
            >
              {data.last_error}
            </pre>
          ) : null}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4">
        <Stat label="Last tick" value={fmtUnix(data.last_tick_at)} testid="crypto-ohlcv-retention-last-tick" />
        <Stat
          label="Sweep age"
          value={fmtDuration(data.seconds_since_last_tick)}
          testid="crypto-ohlcv-retention-age"
        />
        <Stat
          label="Last deleted"
          value={fmtNumber(data.last_deleted)}
          testid="crypto-ohlcv-retention-last-deleted"
        />
        <Stat
          label="Total deleted"
          value={fmtNumber(data.total_deleted)}
          testid="crypto-ohlcv-retention-total-deleted"
        />
        <Stat label="Sweep every" value={fmtInterval(data.interval_seconds)} />
        <Stat label="Started" value={fmtUnix(data.started_at)} />
      </div>

      <div>
        <div className="mb-1 text-[10px] uppercase tracking-widest text-muted-foreground">
          Per-granularity windows
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs" data-testid="crypto-ohlcv-retention-table">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-widest text-muted-foreground">
                <th className="py-1 pr-3">Granularity</th>
                <th className="py-1 pr-3 text-right">Retention (days)</th>
                <th className="py-1 pr-3 text-right">Last sweep deletes</th>
              </tr>
            </thead>
            <tbody>
              {grans.length === 0 ? (
                <tr>
                  <td className="py-2 text-muted-foreground" colSpan={3}>
                    No granularities configured.
                  </td>
                </tr>
              ) : (
                grans.map((g) => (
                  <tr
                    key={g}
                    className="border-t border-border/50"
                    data-testid={`crypto-ohlcv-retention-row-${g}`}
                  >
                    <td className="py-1 pr-3 font-mono uppercase tracking-wider">
                      {g}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {fmtNumber(data.retention_days[g])}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {fmtNumber(lastByGran[g] ?? 0)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  testid,
}: {
  label: string;
  value: string;
  testid?: string;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div
        className="truncate font-mono text-xs text-foreground"
        data-testid={testid}
      >
        {value}
      </div>
    </div>
  );
}
