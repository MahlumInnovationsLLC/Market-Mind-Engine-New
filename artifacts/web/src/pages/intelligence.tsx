import { useMemo } from "react";
import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { TabBar, useTabState, type TabDef } from "@/components/shell/tab-bar";
import SwarmAnalysts from "@/components/telemetry/swarm-analysts";
import SwarmDebates from "@/components/telemetry/swarm-debates";
import ModelInventory from "@/components/telemetry/model-inventory";
import ModelHealthAlerts from "@/components/telemetry/model-health-alerts";
import ActiveModelBadge from "@/components/telemetry/active-model-badge";
import AutoResearchPanel from "@/components/intelligence/autoresearch-panel";
import CalibrationPanel from "@/components/intelligence/calibration-panel";
import EdgePanel from "@/components/intelligence/edge-panel";
import ActivePredictorSelector from "@/components/intelligence/active-predictor-selector";

const TAB_KEYS = ["swarm", "models", "autoresearch", "calibration", "edge"] as const;
type TabKey = (typeof TAB_KEYS)[number];

const TABS: TabDef<TabKey>[] = [
  { key: "swarm", label: "Swarm", subtitle: "6-persona analysts + consensus debates." },
  { key: "models", label: "Models", subtitle: "Model inventory · active predictor · health alerts." },
  { key: "autoresearch", label: "AutoResearch", subtitle: "Discovered candidate features awaiting promotion." },
  { key: "calibration", label: "Calibration", subtitle: "Per-vertical reliability curves." },
  { key: "edge", label: "Edge", subtitle: "Realized vs predicted edge in basis points." },
];

export default function IntelligencePage() {
  const [tab, setTab] = useTabState<TabKey>(TAB_KEYS, "swarm", "/intelligence");
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  const subtitle = useMemo(
    () => TABS.find((t) => t.key === tab)?.subtitle ?? "",
    [tab],
  );

  return (
    <div className="space-y-5 font-mono" data-testid="page-intelligence">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              INTELLIGENCE
            </div>
            <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
              Swarm · Models · Research
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
          </div>
          <ActiveModelBadge />
        </div>
        <TabBar
          tabs={TABS}
          active={tab}
          onSelect={setTab}
          ariaLabel="Intelligence view"
          testIdPrefix="intelligence"
        />
      </header>

      {tab === "swarm" && <SwarmTab />}
      {tab === "models" && <ModelsTab isAdmin={isAdmin} />}
      {tab === "autoresearch" && (
        <div data-testid="panel-intelligence-autoresearch">
          <AutoResearchPanel isAdmin={isAdmin} />
        </div>
      )}
      {tab === "calibration" && (
        <div data-testid="panel-intelligence-calibration">
          <CalibrationPanel />
        </div>
      )}
      {tab === "edge" && (
        <div data-testid="panel-intelligence-edge">
          <EdgePanel />
        </div>
      )}
    </div>
  );
}

function SwarmTab() {
  return (
    <div
      className="grid grid-cols-1 gap-4 xl:grid-cols-2"
      data-testid="panel-intelligence-swarm"
    >
      <GlassCard tone="solid" padding="md" data-testid="card-swarm-analysts">
        <SectionHeader
          eyebrow="ANALYSTS"
          title="Persona scoreboard"
          subtitle="Brier · calibration error · accuracy across the active swarm."
        />
        <SwarmAnalysts />
      </GlassCard>
      <GlassCard tone="solid" padding="md" data-testid="card-swarm-debates">
        <SectionHeader
          eyebrow="DEBATES"
          title="Consensus verdicts"
          subtitle="Most recent moderator-resolved debates · consensus probability + agreement."
        />
        <SwarmDebates />
      </GlassCard>
    </div>
  );
}

function ModelsTab({ isAdmin }: { isAdmin: boolean }) {
  return (
    <div
      className="grid grid-cols-1 gap-4 xl:grid-cols-2"
      data-testid="panel-intelligence-models"
    >
      <GlassCard tone="solid" padding="md" data-testid="card-models-active">
        <SectionHeader
          eyebrow="ACTIVE"
          title="Predictor selector"
          subtitle="Catalog of trained predictors · select to override the live model."
        />
        <ActivePredictorSelector isAdmin={isAdmin} />
      </GlassCard>
      <GlassCard tone="solid" padding="md" data-testid="card-models-health">
        <SectionHeader
          eyebrow="HEALTH"
          title="Open alerts"
          subtitle="Drift / staleness / inference errors raised by the settlement loop."
        />
        <ModelHealthAlerts />
      </GlassCard>
      <GlassCard tone="solid" padding="md" className="xl:col-span-2" data-testid="card-models-inventory">
        <SectionHeader
          eyebrow="INVENTORY"
          title="Cold storage"
          subtitle="Replicated model + source archives backing the active predictor."
        />
        <ModelInventory />
      </GlassCard>
    </div>
  );
}
