import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  AutoResearchDecisionRequestDecision,
  getAdminAutoResearchListFeaturesQueryKey,
  useAdminAutoResearchDecide,
  useAdminAutoResearchListFeatures,
  useAdminAutoResearchRun,
  type AutoResearchFeature,
} from "@workspace/api-client-react";
import { formatErrorMessage } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtNum(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits);
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  const t = new Date(s);
  if (Number.isNaN(t.getTime())) return "—";
  return t.toLocaleString([], {
    hour12: false,
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const STATUS_TONES: Record<string, string> = {
  shadow: "border-warning text-warning",
  promoted: "border-success text-success",
  rejected: "border-destructive text-destructive",
  paused: "border-muted-foreground text-muted-foreground",
  candidate: "border-primary text-primary",
};

function statusTone(status: string | null | undefined): string {
  return STATUS_TONES[(status ?? "").toLowerCase()] ?? "border-border text-muted-foreground";
}

interface RowProps {
  feature: AutoResearchFeature;
  isAdmin: boolean;
  onDecide: (
    name: string,
    decision: keyof typeof AutoResearchDecisionRequestDecision,
  ) => void;
  pending: boolean;
}

function FeatureRow({ feature, isAdmin, onDecide, pending }: RowProps) {
  const tone = statusTone(feature.status);
  return (
    <tr
      className="border-b border-border/40 text-xs hover:bg-muted/20"
      data-testid={`row-autoresearch-${feature.name}`}
    >
      <td className="px-3 py-2">
        <div className="font-medium text-foreground" title={feature.name}>
          {feature.name}
        </div>
        <div className="mt-0.5 text-[10px] uppercase tracking-widest text-muted-foreground">
          {feature.signal_type} · {feature.aggregator}
        </div>
      </td>
      <td className="px-3 py-2 uppercase tracking-widest text-muted-foreground">
        {feature.vertical}
      </td>
      <td className="px-3 py-2">
        <span
          className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest ${tone}`}
          data-testid={`pill-autoresearch-${feature.name}-status`}
        >
          {(feature.status ?? "").toUpperCase()}
        </span>
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(feature.ic)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(feature.sharpe, 2)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{fmtNum(feature.monotonicity, 2)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{feature.n_samples ?? "—"}</td>
      <td className="px-3 py-2 text-right text-[10px] text-muted-foreground">
        {fmtTime(feature.last_evaluated_at)}
      </td>
      <td className="px-3 py-2 text-right">
        {isAdmin ? (
          <div className="flex justify-end gap-1">
            <button
              type="button"
              disabled={pending || feature.status === "production"}
              onClick={() => onDecide(feature.name, "promote")}
              className="border border-success/60 px-2 py-0.5 text-[10px] uppercase tracking-widest text-success hover:bg-success/10 disabled:opacity-40"
              data-testid={`button-promote-${feature.name}`}
            >
              Promote
            </button>
            <button
              type="button"
              disabled={pending || feature.status === "rejected"}
              onClick={() => onDecide(feature.name, "reject")}
              className="border border-destructive/60 px-2 py-0.5 text-[10px] uppercase tracking-widest text-destructive hover:bg-destructive/10 disabled:opacity-40"
              data-testid={`button-reject-${feature.name}`}
            >
              Reject
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => onDecide(feature.name, "reset")}
              className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:opacity-40"
              data-testid={`button-reset-${feature.name}`}
            >
              Reset
            </button>
          </div>
        ) : (
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            ADMIN_ONLY
          </span>
        )}
      </td>
    </tr>
  );
}

export default function AutoResearchPanel({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const queryKey = getAdminAutoResearchListFeaturesQueryKey();

  const { data, isLoading, isError, refetch } = useAdminAutoResearchListFeatures(
    undefined,
    {
      query: { queryKey, refetchInterval: 60_000, enabled: isAdmin },
    },
  );

  const decide = useAdminAutoResearchDecide();
  const run = useAdminAutoResearchRun();
  const [pendingName, setPendingName] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<string | null>(null);

  const onDecide = (
    name: string,
    decision: keyof typeof AutoResearchDecisionRequestDecision,
  ) => {
    setPendingName(name);
    setErrorMsg(null);
    decide.mutate(
      {
        name,
        data: { decision: AutoResearchDecisionRequestDecision[decision] },
      },
      {
        onSuccess: () => {
          setPendingName(null);
          queryClient.invalidateQueries({ queryKey });
        },
        onError: (err) => {
          setPendingName(null);
          setErrorMsg(formatErrorMessage(err, err instanceof Error ? undefined : "Decision failed"));
        },
      },
    );
  };

  const onRunCycle = () => {
    setRunResult(null);
    setErrorMsg(null);
    run.mutate(undefined, {
      onSuccess: (res) => {
        setRunResult(
          `EVAL ${res.candidates_evaluated} · PERSIST ${res.candidates_persisted} · ${res.duration_seconds.toFixed(1)}s`,
        );
        queryClient.invalidateQueries({ queryKey });
      },
      onError: (err) => {
        setErrorMsg(formatErrorMessage(err, err instanceof Error ? undefined : "Run failed"));
      },
    });
  };

  if (!isAdmin) {
    return (
      <GlassCard tone="solid" padding="md" data-testid="card-autoresearch-forbidden">
        <SectionHeader
          eyebrow="AUTORESEARCH"
          title="Admin only"
          subtitle="Feature lifecycle (shadow → promote / reject) requires operator privileges."
        />
        <div className="border border-border bg-card/40 p-4 text-center text-xs uppercase tracking-widest text-muted-foreground">
          ADMIN_ONLY
        </div>
      </GlassCard>
    );
  }

  const items = data?.items ?? [];
  const scheduler = data?.scheduler;

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-autoresearch">
      <SectionHeader
        eyebrow="AUTORESEARCH"
        title="Feature lifecycle"
        subtitle="Discovered candidates · promote/reject decisions · scheduler status."
        actions={
          <div className="flex items-center gap-2">
            {scheduler && (
              <span
                className="text-[10px] uppercase tracking-widest text-muted-foreground"
                data-testid="text-autoresearch-scheduler"
              >
                {scheduler.enabled ? "SCHED ON" : "SCHED OFF"} ·{" "}
                {scheduler.running ? "RUNNING" : "IDLE"} ·{" "}
                {Math.round(scheduler.interval_seconds / 60)}m
              </span>
            )}
            <button
              type="button"
              onClick={onRunCycle}
              disabled={run.isPending}
              className="border border-primary/60 px-3 py-1 text-[10px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:opacity-40"
              data-testid="button-autoresearch-run"
            >
              {run.isPending ? "RUNNING…" : "Run cycle"}
            </button>
            <button
              type="button"
              onClick={() => void refetch()}
              className="border border-border px-3 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
              data-testid="button-autoresearch-refresh"
            >
              Refresh
            </button>
          </div>
        }
      />

      {runResult && (
        <div
          className="mb-2 border border-success/40 bg-success/5 px-3 py-1.5 text-[10px] uppercase tracking-widest text-success"
          data-testid="text-autoresearch-run-result"
        >
          {runResult}
        </div>
      )}
      {errorMsg && (
        <div
          className="mb-2 border border-destructive/40 bg-destructive/5 px-3 py-1.5 text-[10px] uppercase tracking-widest text-destructive"
          data-testid="text-autoresearch-error"
        >
          {errorMsg}
        </div>
      )}

      {isLoading ? (
        <div
          className="h-32 animate-pulse bg-muted/10"
          data-testid="skeleton-autoresearch"
        />
      ) : isError ? (
        <div className="border border-destructive/40 p-4 text-center text-xs uppercase tracking-widest text-destructive">
          ERR_AUTORESEARCH_LOAD
        </div>
      ) : items.length === 0 ? (
        <div className="border border-border bg-card/40 p-4 text-center text-xs uppercase tracking-widest text-muted-foreground">
          NO_CANDIDATES_DISCOVERED
        </div>
      ) : (
        <div className="overflow-auto">
          <table
            className="w-full min-w-[760px] text-xs"
            data-testid="table-autoresearch"
          >
            <thead className="border-b border-border text-[10px] uppercase tracking-widest text-muted-foreground">
              <tr>
                <th className="px-3 py-1 text-left font-normal">Feature</th>
                <th className="px-3 py-1 text-left font-normal">Vertical</th>
                <th className="px-3 py-1 text-left font-normal">Status</th>
                <th className="px-3 py-1 text-right font-normal">IC</th>
                <th className="px-3 py-1 text-right font-normal">Sharpe</th>
                <th className="px-3 py-1 text-right font-normal">Mono</th>
                <th className="px-3 py-1 text-right font-normal">N</th>
                <th className="px-3 py-1 text-right font-normal">Last eval</th>
                <th className="px-3 py-1 text-right font-normal">Decision</th>
              </tr>
            </thead>
            <tbody>
              {items.map((f) => (
                <FeatureRow
                  key={f.id}
                  feature={f}
                  isAdmin={isAdmin}
                  onDecide={onDecide}
                  pending={pendingName === f.name && decide.isPending}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </GlassCard>
  );
}
