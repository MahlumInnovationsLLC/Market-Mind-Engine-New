import { useHealthFull, getHealthFullQueryKey } from "@workspace/api-client-react";
import { Database, HardDrive, KeyRound, Globe, Server } from "lucide-react";

export default function HealthSnapshot() {
  const { data: health, isLoading, isError } = useHealthFull({
    query: { refetchInterval: 30000, queryKey: getHealthFullQueryKey() },
  });

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-48 bg-card" />;
  }

  if (isError || !health) {
    return <div className="border border-destructive p-4 text-destructive h-48 flex items-center justify-center">ERR_SYS_HEALTH_FAIL</div>;
  }

  const isHealthy = health.status === "ok";

  return (
    <div className="border border-border bg-card">
      <div className="border-b border-border p-2 px-3 flex justify-between items-center bg-muted/20">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">SYS_HEALTH</h2>
        <span className={isHealthy ? "text-success" : "text-destructive"}>
          {isHealthy ? "OK" : "WARN"}
        </span>
      </div>
      <div className="p-3 text-xs space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-muted-foreground">
            <Server className="w-3 h-3" />
            <span>MODE</span>
          </div>
          <span className={health.paper_mode ? "text-primary" : "text-destructive"}>
            {health.paper_mode ? "PAPER" : "LIVE"} / {health.kalshi_env.toUpperCase()}
          </span>
        </div>

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-muted-foreground">
            <Database className="w-3 h-3" />
            <span>POSTGRES</span>
          </div>
          <span className={health.postgres?.ok ? "text-success" : "text-destructive"}>
            {health.postgres?.ok ? `OK (${health.postgres.public_tables} TBL)` : "FAIL"}
          </span>
        </div>

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-muted-foreground">
            <HardDrive className="w-3 h-3" />
            <span>STORAGE</span>
          </div>
          <span className={health.storage?.ok ? "text-success" : "text-destructive"}>
            {health.storage?.ok ? "OK" : "FAIL"}
          </span>
        </div>

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-muted-foreground">
            <KeyRound className="w-3 h-3" />
            <span>SECRETS</span>
          </div>
          <div className="flex gap-1">
            <span className={health.secrets_present.openai ? "text-success" : "text-destructive"} title="OpenAI">O</span>
            <span className={health.secrets_present.anthropic ? "text-success" : "text-destructive"} title="Anthropic">A</span>
            <span className={health.secrets_present.kalshi ? "text-success" : "text-destructive"} title="Kalshi">K</span>
          </div>
        </div>

        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-muted-foreground">
            <Globe className="w-3 h-3" />
            <span>SUPABASE</span>
          </div>
          <a 
            href={health.supabase_project} 
            target="_blank" 
            rel="noreferrer"
            className="text-primary hover:underline truncate max-w-[120px]"
          >
            {health.supabase_project.replace("https://", "") || "—"}
          </a>
        </div>
      </div>
    </div>
  );
}
