import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "wouter";
import { ArrowRight, ShieldAlert } from "lucide-react";
import { apiFetch } from "@/lib/api";

type RuntimeSettings = {
  aggression_mode: string;
  trading_halted?: boolean;
  halt_reason?: string | null;
};

export function TradingAlertsBanner() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data } = useQuery<RuntimeSettings>({
    queryKey: ["shell", "runtime-settings"],
    queryFn: () =>
      apiFetch<RuntimeSettings>("/api/dashboard/runtime-settings", {
        getToken: token,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  if (!data?.trading_halted) return null;

  return (
    <div
      className="border-b border-loss/40 bg-loss/10 text-loss px-4 py-2 text-[10px] uppercase tracking-widest font-mono flex items-center gap-2"
      data-testid="banner-trading-halted"
    >
      <ShieldAlert className="w-3.5 h-3.5 shrink-0" />
      <span className="font-bold">TRADING HALTED</span>
      {data.halt_reason ? (
        <span className="opacity-80 normal-case tracking-normal">
          · {data.halt_reason}
        </span>
      ) : null}
      <Link
        href="/system?tab=controls"
        className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded border border-loss/50 hover:bg-loss/20 hover:text-loss focus:outline-none focus:ring-1 focus:ring-loss/60 transition-colors"
        data-testid="link-trading-halted-controls"
      >
        Open kill switch
        <ArrowRight className="w-3 h-3" />
      </Link>
    </div>
  );
}
