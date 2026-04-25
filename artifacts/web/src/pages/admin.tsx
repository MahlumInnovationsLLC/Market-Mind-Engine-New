import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Link, Redirect } from "wouter";
import { useAuth } from "@clerk/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiFetch, REQUEST_ID_HEADER, newRequestId } from "@/lib/api";
import { usePipelineSubscription } from "@/components/realtime/websocket-provider";
import {
  AdminAuditLogEntry,
  AdminUser,
  getAdminGetAuditLogQueryKey,
  getAdminGetUserQueryKey,
  getAdminListUsersQueryKey,
  getGetMyAccessQueryKey,
  useAdminGetUser,
  useAdminListUsers,
  useAdminSetUserAdmin,
  useAdminSetUserApproved,
  useGetVerticalsOverview,
  useAdminGetVerticalVisibility,
  useAdminSetVerticalVisibility,
  getAdminGetVerticalVisibilityQueryKey,
  getGetVerticalsOverviewQueryKey,
  getGetVerticalVisibilityQueryKey,
} from "@workspace/api-client-react";

type Filter = "all" | "pending" | "approved" | "admin";

export default function AdminPage() {
  // /admin has been folded into /system for admins. Keep the path live
  // but redirect operators to the unified Users tab so any bookmarks
  // (or links from older mobile builds) still land somewhere useful.
  return <Redirect to="/system?tab=users" />;
}

export function UserManagementPanel() {
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [activeUserId, setActiveUserId] = useState<string | null>(null);

  const usersQuery = useAdminListUsers({
    query: {
      queryKey: getAdminListUsersQueryKey(),
      refetchInterval: 30_000,
    },
  });

  const items: AdminUser[] = usersQuery.data?.items ?? [];

  const stats = useMemo(() => {
    let total = 0;
    let pending = 0;
    let operators = 0;
    let admins = 0;
    for (const u of items) {
      total += 1;
      if (!u.approved) pending += 1;
      if (u.approved && !u.is_admin) operators += 1;
      if (u.is_admin) admins += 1;
    }
    return { total, pending, operators, admins };
  }, [items]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((u) => {
      if (filter === "pending" && u.approved) return false;
      if (filter === "approved" && !u.approved) return false;
      if (filter === "admin" && !u.is_admin) return false;
      if (q) {
        const hay = `${u.email ?? ""} ${u.display_name ?? ""} ${
          u.clerk_user_id
        }`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [items, filter, search]);

  return (
    <main
      className="grid grid-cols-1 lg:grid-cols-12 gap-6"
      data-testid="panel-user-management"
    >
      <section className="lg:col-span-8 space-y-3">
        <div
          className="grid grid-cols-2 sm:grid-cols-4 gap-2"
          data-testid="admin-stats-strip"
        >
          <StatTile
            label="Total users"
            value={stats.total}
            testId="stat-total-users"
          />
          <StatTile
            label="Pending"
            value={stats.pending}
            testId="stat-pending-approvals"
            tone={stats.pending > 0 ? "warn" : "default"}
          />
          <StatTile
            label="Operators"
            value={stats.operators}
            testId="stat-active-operators"
          />
          <StatTile
            label="Admins"
            value={stats.admins}
            testId="stat-admins"
          />
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs uppercase tracking-widest">
          {(["all", "pending", "approved", "admin"] as const).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              data-testid={`filter-${f}`}
              className={`border px-2 py-1 ${
                filter === f
                  ? "border-primary text-primary"
                  : "border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {f}
            </button>
          ))}
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search email / name / id"
            data-testid="input-admin-search"
            className="ml-auto bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-72 max-w-full"
          />
        </div>

        <div className="border border-border bg-card overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
              <tr>
                <th className="text-left p-2">User</th>
                <th className="text-left p-2">Status</th>
                <th className="text-left p-2">Signed up</th>
                <th className="text-left p-2">Last sign-in</th>
                <th className="text-left p-2">Last seen</th>
                <th className="text-right p-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {usersQuery.isLoading ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    Loading…
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    No users match the current filter.
                  </td>
                </tr>
              ) : (
                filtered.map((u) => (
                  <UserRow
                    key={u.clerk_user_id}
                    user={u}
                    active={u.clerk_user_id === activeUserId}
                    onSelect={() => setActiveUserId(u.clerk_user_id)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <aside className="lg:col-span-4">
        {activeUserId ? (
          <UserDetail
            clerkUserId={activeUserId}
            onClose={() => setActiveUserId(null)}
          />
        ) : (
          <div className="border border-border bg-card p-4 text-xs text-muted-foreground uppercase tracking-widest">
            Select a user to inspect their account.
          </div>
        )}
      </aside>
    </main>
  );
}

type AggressionPendingRevert = {
  at: string;
  target: string;
  from_mode?: string | null;
  by_user_id?: string | null;
  by_email?: string | null;
  reason?: string | null;
};

type AggressionHistoryEntry = {
  at: string | null;
  actor_user_id: string | null;
  actor_email: string | null;
  from_mode: string | null;
  to_mode: string | null;
  reason: string | null;
  scheduled_revert: AggressionPendingRevert | null;
};

type AggressionPreset = {
  id: string;
  label: string;
  target_mode: string;
  reason: string | null;
  revert_minutes: number | null;
  created_at?: string | null;
  created_by_user_id?: string | null;
  created_by_email?: string | null;
};

type RuntimeSettings = {
  aggression_mode: string;
  thresholds: {
    min_edge_pp: number;
    kelly_fraction_cap: number;
    min_agreement_rate: number;
    max_confidence_band: number;
  };
  available_modes: string[];
  updated_at: string | null;
  updated_by: string | null;
  history?: AggressionHistoryEntry[];
  pending_revert?: AggressionPendingRevert | null;
  presets?: AggressionPreset[];
};

function formatRevertMinutes(minutes: number | null | undefined): string {
  if (!minutes || minutes <= 0) return "no auto-revert";
  if (minutes < 60) return `${minutes}m`;
  const hours = minutes / 60;
  if (hours < 24) {
    return Number.isInteger(hours) ? `${hours}h` : `${hours.toFixed(1)}h`;
  }
  const days = hours / 24;
  return Number.isInteger(days) ? `${days}d` : `${days.toFixed(1)}d`;
}

function localDatetimeInput(date: Date): string {
  // datetime-local expects YYYY-MM-DDTHH:MM in browser-local time.
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

export function RuntimePanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "runtime-settings"] as const;
  const [reason, setReason] = useState("");
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [revertAt, setRevertAt] = useState("");
  const [revertTo, setRevertTo] = useState("balanced");
  // Highlight + suggested-mode hint after the operator picks a preset.
  // The preset doesn't auto-apply; clicking the hinted mode button
  // remains the explicit submit so they can still tweak the reason.
  const [pendingPresetMode, setPendingPresetMode] = useState<string | null>(
    null,
  );
  const [pendingPresetLabel, setPendingPresetLabel] = useState<string | null>(
    null,
  );
  // "Save current as preset" form state.
  const [presetFormOpen, setPresetFormOpen] = useState(false);
  const [presetLabel, setPresetLabel] = useState("");
  const [presetTargetMode, setPresetTargetMode] = useState("balanced");
  const [presetRevertMinutes, setPresetRevertMinutes] = useState("");

  const { data, isLoading, error } = useQuery<RuntimeSettings>({
    queryKey,
    queryFn: () =>
      apiFetch<RuntimeSettings>("/api/admin/runtime-settings", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const mutation = useMutation({
    mutationFn: (mode: string) => {
      const body: Record<string, unknown> = { aggression_mode: mode };
      const trimmed = reason.trim();
      if (trimmed) body.reason = trimmed;
      if (scheduleEnabled && revertAt) {
        // datetime-local omits a timezone — interpret in browser local
        // time and forward as ISO so the API stores UTC.
        const dt = new Date(revertAt);
        if (!Number.isNaN(dt.getTime())) {
          body.revert_at = dt.toISOString();
          body.revert_to_mode = revertTo;
        }
      }
      return apiFetch<RuntimeSettings>("/api/admin/runtime-settings", {
        method: "POST",
        body,
        getToken: tokenFn,
      });
    },
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      queryClient.invalidateQueries({
        queryKey: ["dashboard", "runtime-settings"],
      });
      setReason("");
      setScheduleEnabled(false);
      setRevertAt("");
      setPendingPresetMode(null);
      setPendingPresetLabel(null);
    },
  });

  const cancelRevert = useMutation({
    mutationFn: () =>
      apiFetch<RuntimeSettings>("/api/admin/runtime-settings/revert", {
        method: "DELETE",
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      queryClient.invalidateQueries({
        queryKey: ["dashboard", "runtime-settings"],
      });
    },
  });

  const savePreset = useMutation({
    mutationFn: (body: {
      label: string;
      target_mode: string;
      reason?: string;
      revert_minutes?: number;
    }) =>
      apiFetch<RuntimeSettings>("/api/admin/runtime-settings/presets", {
        method: "POST",
        body,
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      setPresetFormOpen(false);
      setPresetLabel("");
      setPresetRevertMinutes("");
    },
  });

  const deletePreset = useMutation({
    mutationFn: (presetId: string) =>
      apiFetch<RuntimeSettings>(
        `/api/admin/runtime-settings/presets/${encodeURIComponent(presetId)}`,
        {
          method: "DELETE",
          getToken: tokenFn,
        },
      ),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
    },
  });

  const applyPreset = (preset: AggressionPreset) => {
    setReason(preset.reason ?? "");
    if (preset.revert_minutes && preset.revert_minutes > 0) {
      const target = new Date(Date.now() + preset.revert_minutes * 60_000);
      setRevertAt(localDatetimeInput(target));
      // Default the auto-revert target to balanced unless the preset
      // already targets balanced (then revert to conservative).
      setRevertTo(preset.target_mode === "balanced" ? "conservative" : "balanced");
      setScheduleEnabled(true);
    } else {
      setScheduleEnabled(false);
      setRevertAt("");
    }
    setPendingPresetMode(preset.target_mode);
    setPendingPresetLabel(preset.label);
  };

  const onSavePresetSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const label = presetLabel.trim();
    if (!label) return;
    const body: {
      label: string;
      target_mode: string;
      reason?: string;
      revert_minutes?: number;
    } = {
      label,
      target_mode: presetTargetMode,
    };
    const trimmedReason = reason.trim();
    if (trimmedReason) body.reason = trimmedReason;
    const minutesRaw = presetRevertMinutes.trim();
    if (minutesRaw) {
      const m = Math.floor(Number(minutesRaw));
      if (Number.isFinite(m) && m > 0) {
        body.revert_minutes = m;
      }
    }
    savePreset.mutate(body);
  };

  return (
    <main className="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <section className="lg:col-span-7 space-y-3">
        <div
          className="border border-border bg-card p-4"
          data-testid="panel-aggression-control"
        >
          <h2 className="text-xs uppercase tracking-widest text-muted-foreground mb-3">
            Aggression mode
          </h2>
          <p className="text-xs text-muted-foreground mb-4 normal-case tracking-normal">
            Sets the global Conservative / Balanced / Aggressive bias for the
            swarm and risk gates. Changes take effect on the next orchestrator
            cycle — the moderator and gatekeeper rebias, and min-edge / Kelly
            defaults retune to match.
          </p>
          {isLoading ? (
            <div className="h-10 animate-pulse bg-muted/30" />
          ) : error ? (
            <div className="text-loss text-xs">
              Failed to load runtime settings.
            </div>
          ) : data ? (
            <div className="space-y-4">
              <div className="flex flex-wrap gap-2">
                {data.available_modes.map((m) => {
                  const active = data.aggression_mode === m;
                  const suggested =
                    pendingPresetMode === m && !active;
                  return (
                    <button
                      key={m}
                      type="button"
                      disabled={mutation.isPending || active}
                      onClick={() => mutation.mutate(m)}
                      data-testid={`button-aggression-${m}`}
                      className={`border px-4 py-2 uppercase text-xs tracking-widest ${
                        active
                          ? "border-primary text-primary bg-primary/10"
                          : suggested
                            ? "border-accent-cyan text-accent-cyan bg-accent-cyan/10"
                            : "border-border text-muted-foreground hover:text-foreground hover:border-foreground"
                      } ${mutation.isPending ? "opacity-50" : ""}`}
                    >
                      {m}
                    </button>
                  );
                })}
              </div>
              <div
                className="space-y-2 border-t border-border pt-3"
                data-testid="panel-aggression-presets"
              >
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    Presets
                  </h3>
                  <button
                    type="button"
                    onClick={() => {
                      setPresetFormOpen((open) => !open);
                      // Default the preset's target mode to the operator's
                      // most recent intent: the active mode if no preset
                      // is loaded, otherwise the loaded preset's target.
                      setPresetTargetMode(
                        pendingPresetMode ?? data.aggression_mode,
                      );
                    }}
                    data-testid="button-toggle-save-preset"
                    className="text-[10px] uppercase tracking-widest border border-border px-2 py-0.5 hover:border-foreground"
                  >
                    {presetFormOpen ? "Cancel" : "Save current as preset"}
                  </button>
                </div>
                {data.presets && data.presets.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {data.presets.map((p) => (
                      <div
                        key={p.id}
                        className="inline-flex items-stretch border border-border bg-background/40"
                        data-testid={`preset-${p.id}`}
                      >
                        <button
                          type="button"
                          onClick={() => applyPreset(p)}
                          data-testid={`button-apply-preset-${p.id}`}
                          title={[
                            `Target: ${p.target_mode.toUpperCase()}`,
                            p.reason ? `Reason: ${p.reason}` : null,
                            `Revert: ${formatRevertMinutes(p.revert_minutes)}`,
                          ]
                            .filter(Boolean)
                            .join("\n")}
                          className="px-3 py-1.5 text-xs hover:bg-primary/10 text-left"
                        >
                          <span className="block font-mono">{p.label}</span>
                          <span className="block text-[9px] uppercase tracking-widest text-muted-foreground">
                            {p.target_mode}
                            {p.revert_minutes
                              ? ` · ${formatRevertMinutes(p.revert_minutes)}`
                              : ""}
                          </span>
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            if (
                              window.confirm(
                                `Delete preset "${p.label}"? This cannot be undone.`,
                              )
                            ) {
                              deletePreset.mutate(p.id);
                            }
                          }}
                          disabled={deletePreset.isPending}
                          data-testid={`button-delete-preset-${p.id}`}
                          className="border-l border-border px-2 text-muted-foreground hover:text-loss text-xs"
                          aria-label={`Delete preset ${p.label}`}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-[11px] text-muted-foreground normal-case tracking-normal">
                    No presets saved yet. Save the routine ones (e.g. “NFL
                    primetime”, “Low liquidity overnight”) so the next
                    operator can apply them in one click.
                  </div>
                )}
                {pendingPresetLabel && pendingPresetMode ? (
                  <div
                    className="text-[11px] text-accent-cyan normal-case tracking-normal"
                    data-testid="text-preset-loaded"
                  >
                    Preset “{pendingPresetLabel}” loaded — review the form
                    below, then click{" "}
                    <span className="uppercase font-mono">
                      {pendingPresetMode}
                    </span>{" "}
                    above to apply.
                    <button
                      type="button"
                      onClick={() => {
                        setPendingPresetMode(null);
                        setPendingPresetLabel(null);
                      }}
                      className="ml-2 underline text-muted-foreground hover:text-foreground"
                      data-testid="button-clear-preset-hint"
                    >
                      clear
                    </button>
                  </div>
                ) : null}
                {presetFormOpen ? (
                  <form
                    onSubmit={onSavePresetSubmit}
                    className="border border-border/60 bg-background/40 p-2 space-y-2"
                    data-testid="form-save-preset"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        type="text"
                        value={presetLabel}
                        onChange={(e) => setPresetLabel(e.target.value)}
                        maxLength={60}
                        placeholder="Preset name (e.g. NFL primetime)"
                        data-testid="input-preset-label"
                        className="flex-1 min-w-[12rem] bg-background border border-border px-2 py-1 text-xs font-mono"
                      />
                      <select
                        value={presetTargetMode}
                        onChange={(e) => setPresetTargetMode(e.target.value)}
                        data-testid="select-preset-target-mode"
                        className="bg-background border border-border px-2 py-1 text-xs uppercase tracking-widest"
                      >
                        {data.available_modes.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                      <input
                        type="number"
                        min={1}
                        max={14 * 24 * 60}
                        step={1}
                        value={presetRevertMinutes}
                        onChange={(e) =>
                          setPresetRevertMinutes(e.target.value)
                        }
                        placeholder="Revert in (min)"
                        data-testid="input-preset-revert-minutes"
                        className="w-32 bg-background border border-border px-2 py-1 text-xs font-mono"
                      />
                      <button
                        type="submit"
                        disabled={
                          savePreset.isPending || !presetLabel.trim()
                        }
                        data-testid="button-save-preset"
                        className="border border-primary text-primary px-3 py-1 text-xs uppercase tracking-widest hover:bg-primary/10 disabled:opacity-50"
                      >
                        {savePreset.isPending ? "Saving…" : "Save"}
                      </button>
                    </div>
                    <p className="text-[10px] text-muted-foreground normal-case tracking-normal">
                      The reason text in the form below is captured with the
                      preset so it auto-fills next time you apply it.
                    </p>
                    {savePreset.isError ? (
                      <div
                        className="text-loss text-[11px]"
                        data-testid="text-save-preset-error"
                      >
                        {savePreset.error instanceof Error
                          ? savePreset.error.message
                          : "Failed to save preset."}
                      </div>
                    ) : null}
                  </form>
                ) : null}
                {deletePreset.isError ? (
                  <div
                    className="text-loss text-[11px]"
                    data-testid="text-delete-preset-error"
                  >
                    Failed to delete preset.
                  </div>
                ) : null}
              </div>
              <div className="space-y-2 border-t border-border pt-3">
                <label
                  htmlFor="aggression-reason"
                  className="block text-[10px] uppercase tracking-widest text-muted-foreground"
                >
                  Reason (optional)
                </label>
                <textarea
                  id="aggression-reason"
                  data-testid="input-aggression-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  maxLength={500}
                  rows={2}
                  placeholder="e.g. NFL primetime — tighten bias for the next 4 hours"
                  className="w-full bg-background border border-border p-2 text-xs font-mono"
                />
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  <label className="inline-flex items-center gap-1.5 text-muted-foreground uppercase tracking-widest text-[10px]">
                    <input
                      type="checkbox"
                      checked={scheduleEnabled}
                      onChange={(e) => setScheduleEnabled(e.target.checked)}
                      data-testid="checkbox-schedule-revert"
                    />
                    Schedule auto-revert
                  </label>
                  {scheduleEnabled ? (
                    <>
                      <input
                        type="datetime-local"
                        value={revertAt}
                        onChange={(e) => setRevertAt(e.target.value)}
                        data-testid="input-revert-at"
                        className="bg-background border border-border px-2 py-1 text-xs font-mono"
                      />
                      <select
                        value={revertTo}
                        onChange={(e) => setRevertTo(e.target.value)}
                        data-testid="select-revert-to"
                        className="bg-background border border-border px-2 py-1 text-xs uppercase tracking-widest"
                      >
                        {data.available_modes.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                    </>
                  ) : null}
                </div>
                <p className="text-[10px] text-muted-foreground normal-case tracking-normal">
                  The reason and schedule attach to the next mode change you
                  pick above; click a mode to apply.
                </p>
              </div>
              {mutation.isError ? (
                <div
                  className="text-loss text-xs"
                  data-testid="text-aggression-error"
                >
                  Failed to update aggression mode.
                </div>
              ) : null}
              {data.pending_revert ? (
                <div
                  className="border border-warn/50 bg-warn/5 p-3 text-xs space-y-1"
                  data-testid="panel-pending-revert"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="uppercase tracking-widest text-warn text-[10px] font-bold">
                      Scheduled auto-revert
                    </span>
                    <button
                      type="button"
                      onClick={() => cancelRevert.mutate()}
                      disabled={cancelRevert.isPending}
                      data-testid="button-cancel-revert"
                      className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest hover:border-foreground"
                    >
                      Cancel
                    </button>
                  </div>
                  <div className="text-muted-foreground normal-case tracking-normal">
                    Reverting to{" "}
                    <span className="text-foreground uppercase">
                      {data.pending_revert.target}
                    </span>{" "}
                    at{" "}
                    <span className="text-foreground font-mono">
                      {data.pending_revert.at}
                    </span>
                    {data.pending_revert.by_email
                      ? ` (set by ${data.pending_revert.by_email})`
                      : null}
                  </div>
                  {data.pending_revert.reason ? (
                    <div className="text-muted-foreground normal-case tracking-normal italic">
                      “{data.pending_revert.reason}”
                    </div>
                  ) : null}
                </div>
              ) : null}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 border-t border-border pt-4 text-xs">
                <ThresholdStat
                  label="MIN_EDGE"
                  value={`${data.thresholds.min_edge_pp.toFixed(1)}pp`}
                />
                <ThresholdStat
                  label="KELLY_CAP"
                  value={`${(data.thresholds.kelly_fraction_cap * 100).toFixed(0)}%`}
                />
                <ThresholdStat
                  label="MIN_AGREE"
                  value={`${(data.thresholds.min_agreement_rate * 100).toFixed(0)}%`}
                />
                <ThresholdStat
                  label="MAX_BAND"
                  value={`${(data.thresholds.max_confidence_band * 100).toFixed(0)}%`}
                />
              </div>
              {data.updated_at ? (
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground border-t border-border pt-3">
                  Updated {data.updated_at}
                  {data.updated_by ? ` by ${data.updated_by}` : ""}
                </div>
              ) : null}
              <div
                className="border-t border-border pt-3"
                data-testid="panel-aggression-history"
              >
                <h3 className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
                  Recent mode changes
                </h3>
                {data.history && data.history.length > 0 ? (
                  <ul className="space-y-2">
                    {data.history.map((h, idx) => (
                      <li
                        key={`${h.at ?? idx}-${idx}`}
                        className="text-xs border border-border/60 bg-background/40 p-2"
                        data-testid={`row-aggression-history-${idx}`}
                      >
                        <div className="flex flex-wrap items-baseline justify-between gap-2">
                          <div className="font-mono text-[11px]">
                            <span className="text-muted-foreground uppercase">
                              {h.from_mode ?? "—"}
                            </span>
                            <span className="mx-1 text-muted-foreground">→</span>
                            <span className="text-primary uppercase">
                              {h.to_mode ?? "—"}
                            </span>
                          </div>
                          <div className="text-[10px] uppercase tracking-widest text-muted-foreground font-mono">
                            {h.at ?? "—"}
                          </div>
                        </div>
                        <div className="text-[10px] text-muted-foreground mt-1 normal-case tracking-normal">
                          by {h.actor_email ?? h.actor_user_id ?? "system"}
                        </div>
                        {h.reason ? (
                          <div className="text-xs text-foreground mt-1 normal-case tracking-normal italic">
                            “{h.reason}”
                          </div>
                        ) : null}
                        {h.scheduled_revert ? (
                          <div className="text-[10px] text-warn mt-1 normal-case tracking-normal">
                            Scheduled revert to{" "}
                            {h.scheduled_revert.target.toUpperCase()} at{" "}
                            <span className="font-mono">
                              {h.scheduled_revert.at}
                            </span>
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-xs text-muted-foreground normal-case tracking-normal">
                    No changes recorded yet.
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </section>
    </main>
  );
}

type GatekeeperModelOption = {
  provider: string;
  model_id: string;
  label: string;
  secret_env: string;
  available: boolean;
};

type GatekeeperModelState = {
  selected_provider: string | null;
  selected_model_id: string | null;
  default_provider: string;
  default_model_id: string;
  options: GatekeeperModelOption[];
  updated_at: string | null;
  updated_by: string | null;
};

function optionKey(o: { provider: string; model_id: string }): string {
  return `${o.provider}::${o.model_id}`;
}

export function GatekeeperModelPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "runtime-settings", "gatekeeper-model"] as const;

  const { data, isLoading, error } = useQuery<GatekeeperModelState>({
    queryKey,
    queryFn: () =>
      apiFetch<GatekeeperModelState>(
        "/api/admin/runtime-settings/gatekeeper-model",
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  // Prefer the active override; fall back to the persona default so the
  // dropdown always reflects what the runner will *actually* use on the
  // next debate.
  const effectiveKey =
    data == null
      ? ""
      : optionKey({
          provider: data.selected_provider ?? data.default_provider,
          model_id: data.selected_model_id ?? data.default_model_id,
        });
  const [pendingKey, setPendingKey] = useState<string>("");

  // Reset the staged selection whenever the server snapshot changes so
  // the form doesn't show a stale value after another operator's save.
  useEffect(() => {
    setPendingKey(effectiveKey);
  }, [effectiveKey]);

  const mutation = useMutation({
    mutationFn: (key: string) => {
      const opt = data?.options.find((o) => optionKey(o) === key);
      // Empty key (or "default") clears the override.
      const isDefault =
        !opt ||
        (opt.provider === data?.default_provider &&
          opt.model_id === data?.default_model_id);
      const body = isDefault
        ? { provider: null, model_id: null }
        : { provider: opt.provider, model_id: opt.model_id };
      return apiFetch<GatekeeperModelState>(
        "/api/admin/runtime-settings/gatekeeper-model",
        { method: "POST", body, getToken: tokenFn },
      );
    },
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
    },
  });

  if (!isSignedIn) return null;

  return (
    <main className="terminal-screen px-6 pt-6 pb-12">
      <section
        className="terminal-card p-6 max-w-3xl"
        data-testid="panel-gatekeeper-model"
      >
        <header className="border-b border-border pb-3 mb-4">
          <div className="flex items-baseline justify-between">
            <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
              Gatekeeper Model
            </h2>
            {data ? (
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground font-mono">
                {data.selected_provider ? "OVERRIDE ACTIVE" : "DEFAULT"}
              </div>
            ) : null}
          </div>
          <p className="text-xs text-muted-foreground normal-case tracking-normal mt-2">
            Picks the LLM that vetoes trades before they reach the order
            pipe. Switching takes effect on the next debate — no
            restart. Providers without a configured API key are
            disabled.
          </p>
        </header>

        {isLoading ? (
          <div className="text-xs text-muted-foreground">Loading…</div>
        ) : error ? (
          <div
            className="text-xs text-destructive"
            data-testid="text-gatekeeper-model-error"
          >
            Failed to load gatekeeper model state.
          </div>
        ) : data ? (
          <div className="space-y-4">
            {(() => {
              if (!data.selected_provider || !data.selected_model_id) return null;
              const active = data.options.find(
                (o) =>
                  o.provider === data.selected_provider &&
                  o.model_id === data.selected_model_id,
              );
              if (!active || active.available) return null;
              return (
                <div
                  className="border border-destructive/60 bg-destructive/10 px-3 py-2 text-xs font-mono"
                  data-testid="banner-gatekeeper-override-unavailable"
                >
                  <div className="uppercase tracking-widest text-destructive text-[10px] mb-1">
                    Override unavailable — gatekeeper will fail closed
                  </div>
                  <div className="text-foreground normal-case tracking-normal">
                    The active override <span className="text-primary">{data.selected_provider}/{data.selected_model_id}</span>{" "}
                    has no API key configured ({active.secret_env}).
                    Every debate will fail closed (no trades) until you
                    revert to the persona default or add the key.
                  </div>
                  <button
                    type="button"
                    data-testid="button-force-revert-gatekeeper-model"
                    disabled={mutation.isPending}
                    onClick={() => {
                      const defKey = optionKey({
                        provider: data.default_provider,
                        model_id: data.default_model_id,
                      });
                      setPendingKey(defKey);
                      mutation.mutate(defKey);
                    }}
                    className="mt-2 terminal-button-primary px-3 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50"
                  >
                    Revert to default now
                  </button>
                </div>
              );
            })()}
            <div>
              <label
                htmlFor="gatekeeper-model-select"
                className="text-[10px] uppercase tracking-widest text-muted-foreground block mb-2"
              >
                Active model
              </label>
              <select
                id="gatekeeper-model-select"
                data-testid="select-gatekeeper-model"
                className="w-full bg-background border border-border px-3 py-2 text-sm font-mono"
                value={pendingKey}
                onChange={(e) => setPendingKey(e.target.value)}
              >
                {data.options.map((o) => {
                  const isDefault =
                    o.provider === data.default_provider &&
                    o.model_id === data.default_model_id;
                  const suffix = !o.available
                    ? `  (${o.secret_env} not configured)`
                    : isDefault
                    ? "  (default)"
                    : "";
                  return (
                    <option
                      key={optionKey(o)}
                      value={optionKey(o)}
                      disabled={!o.available}
                      data-testid={`option-gatekeeper-${o.provider}-${o.model_id}`}
                    >
                      {o.label}
                      {suffix}
                    </option>
                  );
                })}
              </select>
            </div>

            <div className="text-xs text-muted-foreground font-mono normal-case tracking-normal">
              <span className="uppercase text-[10px] tracking-widest">
                Currently active:
              </span>{" "}
              <span className="text-primary">
                {data.selected_provider ?? data.default_provider}
              </span>
              {" / "}
              <span className="text-primary">
                {data.selected_model_id ?? data.default_model_id}
              </span>
              {data.selected_provider ? null : (
                <span className="ml-2 text-[10px] uppercase tracking-widest">
                  (persona default)
                </span>
              )}
            </div>

            <div className="flex gap-2">
              <button
                type="button"
                data-testid="button-save-gatekeeper-model"
                disabled={
                  mutation.isPending || pendingKey === effectiveKey
                }
                onClick={() => mutation.mutate(pendingKey)}
                className="terminal-button-primary px-4 py-2 text-xs uppercase tracking-widest disabled:opacity-50"
              >
                {mutation.isPending ? "Saving…" : "Save"}
              </button>
              {data.selected_provider ? (
                <button
                  type="button"
                  data-testid="button-reset-gatekeeper-model"
                  disabled={mutation.isPending}
                  onClick={() => {
                    const defKey = optionKey({
                      provider: data.default_provider,
                      model_id: data.default_model_id,
                    });
                    setPendingKey(defKey);
                    mutation.mutate(defKey);
                  }}
                  className="terminal-button px-4 py-2 text-xs uppercase tracking-widest disabled:opacity-50"
                >
                  Revert to default
                </button>
              ) : null}
            </div>

            {mutation.isError ? (
              <div
                className="text-xs text-destructive"
                data-testid="text-gatekeeper-model-save-error"
              >
                Save failed:{" "}
                {(mutation.error as Error | undefined)?.message ?? "unknown"}
              </div>
            ) : null}

            {data.updated_at ? (
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground border-t border-border pt-3">
                Updated {data.updated_at}
                {data.updated_by ? ` by ${data.updated_by}` : ""}
              </div>
            ) : null}
          </div>
        ) : null}
      </section>
    </main>
  );
}

type TechAiPromotionStatus = {
  settled_count: number;
  brier: number | null;
  eligible: boolean;
  current_multiplier: number;
  reason: string;
};

export function TechAiPromotionPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "tech-ai", "promotion-status"] as const;

  const { data, isLoading, error } = useQuery<TechAiPromotionStatus>({
    queryKey,
    queryFn: () =>
      apiFetch<TechAiPromotionStatus>(
        "/api/admin/tech-ai/promotion-status",
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
  });

  const promote = useMutation({
    mutationFn: () =>
      apiFetch<TechAiPromotionStatus>("/api/admin/tech-ai/promote-kelly", {
        method: "POST",
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
    },
  });

  const onPromote = () => {
    if (!data?.eligible) return;
    if (
      !window.confirm(
        "Promote tech-AI Kelly multiplier from 0.06 to 0.10? This change is logged.",
      )
    ) {
      return;
    }
    promote.mutate();
  };

  const brierText =
    data?.brier === null || data?.brier === undefined
      ? "—"
      : data.brier.toFixed(4);
  const multiplierText =
    data?.current_multiplier === undefined
      ? "—"
      : data.current_multiplier.toFixed(2);

  return (
    <div
      className="space-y-3"
      data-testid="panel-tech-ai-promotion"
    >
      <p className="text-[11px] text-muted-foreground leading-snug">
        Tech-AI ships at a paper-grade Kelly multiplier of <code>0.06</code>.
        Promotion to the production multiplier <code>0.10</code> unlocks once
        the rolling Brier on the latest settled trades clears the threshold
        (≥30 settled trades, Brier &lt; 0.20).
      </p>

      {isLoading ? (
        <div className="h-10 animate-pulse bg-muted/30" />
      ) : error || !data ? (
        <div
          className="text-loss text-xs"
          data-testid="text-tech-ai-promotion-error"
        >
          Failed to load tech-AI promotion status.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 text-[11px] md:grid-cols-4">
            <div data-testid="tech-ai-settled-count">
              <div className="text-muted-foreground uppercase tracking-wider">
                Settled trades
              </div>
              <div className="font-mono text-sm">{data.settled_count}</div>
              <div className="text-[10px] text-muted-foreground">
                need ≥ 30
              </div>
            </div>
            <div data-testid="tech-ai-brier">
              <div className="text-muted-foreground uppercase tracking-wider">
                Rolling Brier
              </div>
              <div className="font-mono text-sm">{brierText}</div>
              <div className="text-[10px] text-muted-foreground">
                need &lt; 0.20
              </div>
            </div>
            <div data-testid="tech-ai-current-multiplier">
              <div className="text-muted-foreground uppercase tracking-wider">
                Kelly multiplier
              </div>
              <div className="font-mono text-sm">{multiplierText}</div>
              <div className="text-[10px] text-muted-foreground">
                target 0.10
              </div>
            </div>
            <div data-testid="tech-ai-eligibility">
              <div className="text-muted-foreground uppercase tracking-wider">
                Eligibility
              </div>
              <div
                className={`font-mono text-sm ${
                  data.eligible ? "text-accent-cyan" : "text-muted-foreground"
                }`}
              >
                {data.eligible ? "ELIGIBLE" : "NOT YET"}
              </div>
            </div>
          </div>

          <div
            className="border border-border bg-background/40 px-3 py-2 text-[11px] text-muted-foreground normal-case tracking-normal"
            data-testid="tech-ai-reason"
          >
            {data.reason}
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={onPromote}
              disabled={!data.eligible || promote.isPending}
              data-testid="button-tech-ai-promote"
              className={`border px-4 py-2 uppercase text-xs tracking-widest ${
                data.eligible
                  ? "border-primary text-primary hover:bg-primary/10"
                  : "border-border text-muted-foreground cursor-not-allowed"
              } ${promote.isPending ? "opacity-50" : ""}`}
            >
              {promote.isPending ? "Promoting…" : "Promote to 0.10"}
            </button>
            {promote.isError ? (
              <span
                className="text-loss text-xs"
                data-testid="text-tech-ai-promote-error"
              >
                Promotion failed.
              </span>
            ) : null}
            {promote.isSuccess && data.current_multiplier >= 0.10 ? (
              <span
                className="text-accent-cyan text-xs"
                data-testid="text-tech-ai-promote-success"
              >
                Promoted.
              </span>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}

function StatTile({
  label,
  value,
  testId,
  tone = "default",
}: {
  label: string;
  value: number;
  testId: string;
  tone?: "default" | "warn";
}) {
  const valueClass =
    tone === "warn" && value > 0 ? "text-destructive" : "text-foreground";
  return (
    <div
      className="border border-border bg-card px-3 py-2"
      data-testid={testId}
    >
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className={`text-2xl font-bold ${valueClass}`}>{value}</div>
    </div>
  );
}

function ThresholdStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className="text-sm font-bold text-foreground">{value}</div>
    </div>
  );
}

export function StatusPills({ user }: { user: AdminUser }) {
  return (
    <div className="flex flex-wrap gap-1">
      {user.is_admin ? (
        <span
          className="border border-primary text-primary px-1.5 py-0.5 text-[10px] uppercase tracking-widest"
          data-testid={`pill-admin-${user.clerk_user_id}`}
        >
          Admin
        </span>
      ) : null}
      {user.approved ? (
        <span
          className="border border-success text-success px-1.5 py-0.5 text-[10px] uppercase tracking-widest"
          data-testid={`pill-approved-${user.clerk_user_id}`}
        >
          Approved
        </span>
      ) : (
        <span
          className="border border-destructive text-destructive px-1.5 py-0.5 text-[10px] uppercase tracking-widest"
          data-testid={`pill-pending-${user.clerk_user_id}`}
        >
          Pending
        </span>
      )}
    </div>
  );
}

export function UserRow({
  user,
  active,
  onSelect,
}: {
  user: AdminUser;
  active: boolean;
  onSelect: () => void;
}) {
  const qc = useQueryClient();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: getAdminListUsersQueryKey() });
    qc.invalidateQueries({
      queryKey: getAdminGetUserQueryKey(user.clerk_user_id),
    });
    qc.invalidateQueries({ queryKey: getAdminGetAuditLogQueryKey() });
    qc.invalidateQueries({ queryKey: getGetMyAccessQueryKey() });
  };
  const setApproved = useAdminSetUserApproved({
    mutation: { onSuccess: refresh },
  });
  const setAdmin = useAdminSetUserAdmin({ mutation: { onSuccess: refresh } });

  const onApproveToggle = () => {
    const next = !user.approved;
    const verb = next ? "approve" : "revoke approval for";
    if (!window.confirm(`Are you sure you want to ${verb} ${user.email ?? user.clerk_user_id}?`))
      return;
    setApproved.mutate({ clerkUserId: user.clerk_user_id, data: { approved: next } });
  };

  const onAdminToggle = () => {
    const next = !user.is_admin;
    const verb = next ? "promote to admin" : "demote from admin";
    if (!window.confirm(`Are you sure you want to ${verb}: ${user.email ?? user.clerk_user_id}?`))
      return;
    setAdmin.mutate({ clerkUserId: user.clerk_user_id, data: { is_admin: next } });
  };

  return (
    <tr
      className={`border-t border-border ${active ? "bg-muted/20" : "hover:bg-muted/10"} cursor-pointer scroll-mt-24 data-[focus-flash=true]:outline data-[focus-flash=true]:outline-2 data-[focus-flash=true]:outline-accent-cyan data-[focus-flash=true]:bg-accent-cyan/10`}
      onClick={onSelect}
      data-focus-id={`user-${user.clerk_user_id}`}
      data-testid={`row-user-${user.clerk_user_id}`}
    >
      <td className="p-2 align-top">
        <div className="text-foreground" data-testid={`text-user-email-${user.clerk_user_id}`}>
          {user.email ?? "(no email)"}
        </div>
        <div className="text-[10px] text-muted-foreground truncate max-w-[200px]">
          {user.display_name ?? user.clerk_user_id}
        </div>
      </td>
      <td className="p-2 align-top">
        <StatusPills user={user} />
        <div className="text-[10px] text-muted-foreground mt-1">
          seen {user.activity_count}x
        </div>
      </td>
      <td className="p-2 align-top text-muted-foreground">
        {user.clerk_created_at?.slice(0, 10) ?? "—"}
      </td>
      <td className="p-2 align-top text-muted-foreground">
        {user.clerk_last_sign_in_at?.replace("T", " ").replace("Z", "") ?? "—"}
      </td>
      <td className="p-2 align-top text-muted-foreground">
        {user.last_seen_at?.replace("T", " ").replace("Z", "") ?? "—"}
      </td>
      <td className="p-2 align-top">
        <div className="flex flex-wrap gap-1 justify-end" onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            onClick={onApproveToggle}
            disabled={setApproved.isPending}
            data-testid={`button-approve-${user.clerk_user_id}`}
            className={`border px-2 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50 ${
              user.approved
                ? "border-destructive text-destructive hover:bg-destructive/10"
                : "border-success text-success hover:bg-success/10"
            }`}
          >
            {user.approved ? "Revoke" : "Approve"}
          </button>
          <button
            type="button"
            onClick={onAdminToggle}
            disabled={setAdmin.isPending}
            data-testid={`button-admin-${user.clerk_user_id}`}
            className={`border px-2 py-1 text-[10px] uppercase tracking-widest disabled:opacity-50 ${
              user.is_admin
                ? "border-destructive text-destructive hover:bg-destructive/10"
                : "border-primary text-primary hover:bg-primary/10"
            }`}
          >
            {user.is_admin ? "Demote" : "Promote"}
          </button>
        </div>
      </td>
    </tr>
  );
}

export function UserDetail({
  clerkUserId,
  onClose,
}: {
  clerkUserId: string;
  onClose: () => void;
}) {
  const { data, isLoading, isError } = useAdminGetUser(clerkUserId, {
    query: { queryKey: getAdminGetUserQueryKey(clerkUserId) },
  });
  return (
    <div className="border border-border bg-card" data-testid="panel-user-detail">
      <div className="flex items-center justify-between border-b border-border p-2 px-3 bg-muted/20">
        <h2 className="text-xs uppercase tracking-widest text-muted-foreground">User detail</h2>
        <button
          type="button"
          onClick={onClose}
          className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-destructive"
          data-testid="button-close-detail"
        >
          ✕
        </button>
      </div>
      <div className="p-3 text-xs space-y-3">
        {isLoading ? (
          <div className="text-muted-foreground">Loading…</div>
        ) : isError || !data ? (
          <div className="text-destructive">Failed to load user.</div>
        ) : (
          <>
            <Field label="Email" value={data.email ?? "—"} />
            <Field label="Display name" value={data.display_name ?? "—"} />
            <Field label="Clerk user id" value={data.clerk_user_id} />
            <Field label="Approval source" value={data.approval_source} />
            <Field label="Activity count" value={String(data.activity_count)} />
            <Field label="First seen" value={data.first_seen_at ?? "—"} />
            <Field label="Last seen" value={data.last_seen_at ?? "—"} />
            <Field
              label="Signed up (Clerk)"
              value={
                ((data.clerk_profile as Record<string, unknown> | undefined)?.created_at as
                  | number
                  | undefined)
                  ? new Date(
                      (data.clerk_profile as Record<string, number>).created_at,
                    ).toISOString()
                  : "—"
              }
            />
            <Field
              label="Last sign-in (Clerk)"
              value={
                ((data.clerk_profile as Record<string, unknown> | undefined)?.last_sign_in_at as
                  | number
                  | undefined)
                  ? new Date(
                      (data.clerk_profile as Record<string, number>).last_sign_in_at,
                    ).toISOString()
                  : "—"
              }
            />
            <div className="border-t border-border pt-2 mt-2">
              <div className="text-muted-foreground uppercase tracking-widest text-[10px] mb-1">
                Clerk profile
              </div>
              <pre className="text-[10px] leading-tight bg-background/40 border border-border p-2 max-h-64 overflow-auto">
{JSON.stringify(data.clerk_profile ?? {}, null, 2)}
              </pre>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-muted-foreground uppercase tracking-widest text-[10px]">
        {label}
      </span>
      <span className="text-foreground truncate max-w-[180px]" title={value}>
        {value}
      </span>
    </div>
  );
}

export function AuditLogPanel() {
  // Server-side pagination (added per UI overhaul code review): we drive
  // /api/admin/audit-log with `limit` + `offset` query params via raw
  // apiFetch so we don't have to regen the typed client just to add an
  // offset arg. The endpoint returns `{ items, total, limit, offset }`;
  // `total` is the number of rows matching the active filters (independent
  // of paging) so we can render "Showing N–M of T" and disable Next on the
  // actual last page.
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const PAGE_SIZES = [25, 50, 100, 200] as const;
  const [pageSize, setPageSize] = useState<number>(50);
  const [page, setPage] = useState<number>(0); // zero-indexed

  // Filter inputs are split into "draft" (the live <input> value) and
  // "applied" (what the query actually sends). This avoids re-fetching on
  // every keystroke and lets the operator hit Enter / blur to commit.
  const [targetDraft, setTargetDraft] = useState("");
  const [actorDraft, setActorDraft] = useState("");
  const [actionApplied, setActionApplied] = useState("");
  const [targetApplied, setTargetApplied] = useState("");
  const [actorApplied, setActorApplied] = useState("");

  const applyFilters = () => {
    setTargetApplied(targetDraft.trim());
    setActorApplied(actorDraft.trim());
    setPage(0);
  };
  const clearFilters = () => {
    setTargetDraft("");
    setActorDraft("");
    setTargetApplied("");
    setActorApplied("");
    setActionApplied("");
    setPage(0);
  };

  type AuditPage = {
    items: AdminAuditLogEntry[];
    total?: number | null;
    limit: number;
    offset: number;
  };
  const offset = page * pageSize;
  const buildFilterParams = () => {
    const qs = new URLSearchParams();
    if (targetApplied) qs.set("target", targetApplied);
    if (actorApplied) qs.set("actor", actorApplied);
    if (actionApplied) qs.set("action", actionApplied);
    return qs;
  };
  const buildUrl = () => {
    const qs = buildFilterParams();
    qs.set("limit", String(pageSize));
    qs.set("offset", String(offset));
    return `/api/admin/audit-log?${qs.toString()}`;
  };
  const query = useQuery<AuditPage>({
    queryKey: [
      "admin",
      "audit-log",
      pageSize,
      offset,
      targetApplied,
      actorApplied,
      actionApplied,
    ],
    queryFn: () => apiFetch<AuditPage>(buildUrl(), { getToken: tokenFn }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });

  type ActionsList = { items: string[] };
  const actionsQuery = useQuery<ActionsList>({
    queryKey: ["admin", "audit-log", "actions"],
    queryFn: () =>
      apiFetch<ActionsList>("/api/admin/audit-log/actions", { getToken: tokenFn }),
    enabled: !!isSignedIn,
    staleTime: 60_000,
  });
  const knownActions = actionsQuery.data?.items ?? [];
  const hasActiveFilters = !!(targetApplied || actorApplied || actionApplied);

  const items: AdminAuditLogEntry[] = query.data?.items ?? [];
  // `total` may be missing on older deploys / error responses; fall back to
  // the cheap heuristic in that case so the Next button still behaves.
  const totalRaw = query.data?.total;
  const total: number | null =
    typeof totalRaw === "number" && Number.isFinite(totalRaw) ? totalRaw : null;
  const hasMore =
    total != null ? offset + items.length < total : items.length === pageSize;
  const isLoading = query.isLoading;
  const isError = query.isError;

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const onDownloadCsv = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const qs = buildFilterParams();
      const url = `/api/admin/audit-log.csv${qs.size > 0 ? `?${qs.toString()}` : ""}`;
      const headers = new Headers({
        accept: "text/csv",
        [REQUEST_ID_HEADER]: newRequestId(),
      });
      const token = await tokenFn();
      if (token) headers.set("authorization", `Bearer ${token}`);
      const res = await fetch(url, { headers });
      if (!res.ok) {
        const id = res.headers.get(REQUEST_ID_HEADER);
        const base = `HTTP ${res.status} ${res.statusText}`;
        throw new Error(id ? `${base} (Reference: ${id})` : base);
      }
      const blob = await res.blob();
      const dispo = res.headers.get("content-disposition") ?? "";
      const match = /filename="?([^";]+)"?/i.exec(dispo);
      const filename = match?.[1] ?? "axiom-audit-log.csv";
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
    } catch (e) {
      setDownloadError((e as Error)?.message ?? "Download failed.");
    } finally {
      setDownloading(false);
    }
  };

  const goPrev = () => setPage((p) => Math.max(0, p - 1));
  const goNext = () => {
    if (hasMore) setPage((p) => p + 1);
  };
  const onChangePageSize = (next: number) => {
    setPageSize(next);
    setPage(0);
  };

  const rangeStart = items.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;
  const totalLabel = total != null ? ` of ${total.toLocaleString()}` : "";

  return (
    <div className="space-y-3" data-testid="panel-audit-log">
      <div className="flex flex-wrap items-end gap-2 text-[11px] uppercase tracking-widest text-muted-foreground border border-border bg-card p-2">
        <label className="flex flex-col gap-1">
          <span>Target user</span>
          <input
            value={targetDraft}
            onChange={(e) => setTargetDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyFilters();
            }}
            onBlur={() => {
              if (targetDraft.trim() !== targetApplied) applyFilters();
            }}
            placeholder="email or user id"
            data-testid="input-audit-filter-target"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-56 max-w-full normal-case text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>Actor</span>
          <input
            value={actorDraft}
            onChange={(e) => setActorDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applyFilters();
            }}
            onBlur={() => {
              if (actorDraft.trim() !== actorApplied) applyFilters();
            }}
            placeholder="email or user id"
            data-testid="input-audit-filter-actor"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-56 max-w-full normal-case text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>Action</span>
          <select
            value={actionApplied}
            onChange={(e) => {
              setActionApplied(e.target.value);
              setPage(0);
            }}
            data-testid="select-audit-filter-action"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal text-foreground min-w-[10rem]"
          >
            <option value="">all actions</option>
            {knownActions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={applyFilters}
          data-testid="button-audit-apply-filters"
          className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-foreground hover:bg-muted/30"
        >
          Apply
        </button>
        <button
          type="button"
          onClick={clearFilters}
          disabled={!hasActiveFilters && !targetDraft && !actorDraft}
          data-testid="button-audit-clear-filters"
          className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
        >
          Clear
        </button>
        <button
          type="button"
          onClick={onDownloadCsv}
          disabled={downloading || isLoading}
          data-testid="button-audit-download-csv"
          className="border border-primary px-2 py-1 text-[11px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {downloading ? "Exporting…" : "Download CSV"}
        </button>
        {hasActiveFilters ? (
          <span
            className="text-primary text-[10px] tracking-widest"
            data-testid="text-audit-filter-active"
          >
            filters on
          </span>
        ) : null}
      </div>
      {downloadError ? (
        <div
          className="text-destructive text-xs"
          data-testid="text-audit-download-error"
        >
          {downloadError}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] uppercase tracking-widest text-muted-foreground">
        <div data-testid="text-audit-range">
          {isLoading && !query.data
            ? "Loading…"
            : isError
              ? "—"
              : `Showing ${rangeStart}–${rangeEnd}${totalLabel} (page ${page + 1})`}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1">
            <span>Page size</span>
            <select
              value={pageSize}
              onChange={(e) => onChangePageSize(Number(e.target.value))}
              className="border border-border bg-card px-1 py-0.5 text-[11px] uppercase tracking-widest text-foreground"
              data-testid="select-audit-page-size"
            >
              {PAGE_SIZES.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={goPrev}
            disabled={page === 0 || isLoading}
            className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="button-audit-prev"
          >
            ← Prev
          </button>
          <button
            type="button"
            onClick={goNext}
            disabled={!hasMore || isLoading}
            className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="button-audit-next"
          >
            Next →
          </button>
        </div>
      </div>
      <div className="border border-border bg-card overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
            <tr>
              <th className="text-left p-2">When</th>
              <th className="text-left p-2">Actor</th>
              <th className="text-left p-2">Target</th>
              <th className="text-left p-2">Action</th>
              <th className="text-left p-2">Before → After</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && !query.data ? (
              <tr>
                <td colSpan={5} className="p-6 text-center text-muted-foreground">
                  Loading…
                </td>
              </tr>
            ) : isError ? (
              <tr>
                <td colSpan={5} className="p-6 text-center text-destructive">
                  Failed to load audit log.
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td colSpan={5} className="p-6 text-center text-muted-foreground">
                  {hasActiveFilters
                    ? "No audit entries match the current filters."
                    : page === 0
                      ? "No audit entries yet."
                      : "No more entries."}
                </td>
              </tr>
            ) : (
              items.map((e) => (
                <tr
                  key={e.id}
                  className="border-t border-border scroll-mt-24 data-[focus-flash=true]:outline data-[focus-flash=true]:outline-2 data-[focus-flash=true]:outline-accent-cyan data-[focus-flash=true]:bg-accent-cyan/10"
                  data-focus-id={`audit-${e.id}`}
                  data-testid={`row-audit-${e.id}`}
                >
                  <td className="p-2 align-top text-muted-foreground">
                    {e.created_at?.replace("T", " ").replace("Z", "") ?? "—"}
                  </td>
                  <td className="p-2 align-top">{e.actor_email ?? e.actor_user_id}</td>
                  <td className="p-2 align-top">{e.target_email ?? e.target_user_id}</td>
                  <td className="p-2 align-top text-primary uppercase tracking-widest text-[10px]">
                    {e.action}
                  </td>
                  <td className="p-2 align-top text-[10px] text-muted-foreground">
                    {JSON.stringify(e.before_value ?? {})} → {JSON.stringify(e.after_value ?? {})}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Risk knob editor panel
// ---------------------------------------------------------------------------

type RiskKnob = {
  key: string;
  label: string;
  description: string;
  minimum: number;
  maximum: number;
  step: number;
  kind: "float" | "int";
  unit: "pp" | "fraction" | "usd" | "count";
  default_value: number | null;
  override_value: number | null;
  effective_value: number | null;
};

type RiskKnobState = {
  aggression_mode: string;
  knobs: RiskKnob[];
};

function formatKnob(value: number | null | undefined, unit: RiskKnob["unit"]) {
  if (value == null) return "—";
  if (unit === "pp") return `${value.toFixed(1)}pp`;
  if (unit === "fraction") return `${(value * 100).toFixed(0)}%`;
  if (unit === "usd")
    return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  return String(value);
}

type RiskKnobHistoryEntry = {
  at: string | null;
  actor_user_id: string | null;
  actor_email: string | null;
  before: number | null;
  after: number | null;
};

type RiskKnobHistoryResponse = {
  history: Record<string, RiskKnobHistoryEntry[]>;
};

function KnobHistoryList({
  knobKey,
  unit,
  entries,
  loading,
}: {
  knobKey: string;
  unit: RiskKnob["unit"];
  entries: RiskKnobHistoryEntry[];
  loading: boolean;
}) {
  return (
    <div
      className="mt-3 border-t border-border/60 pt-2"
      data-testid={`knob-history-${knobKey}`}
    >
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-1">
        Recent changes
      </div>
      {loading ? (
        <div className="h-4 w-32 animate-pulse bg-muted/30" />
      ) : entries.length === 0 ? (
        <div className="text-[10px] text-muted-foreground normal-case tracking-normal">
          No recorded changes yet.
        </div>
      ) : (
        <ul className="space-y-1">
          {entries.map((h, idx) => (
            <li
              key={`${h.at ?? idx}-${idx}`}
              className="flex flex-wrap items-baseline gap-x-2 text-[11px] font-mono"
              data-testid={`knob-history-${knobKey}-row-${idx}`}
            >
              <span className="text-muted-foreground">{h.at ?? "—"}</span>
              <span className="text-muted-foreground">
                {formatKnob(h.before ?? null, unit)}
              </span>
              <span className="text-muted-foreground">→</span>
              <span className="text-primary">
                {formatKnob(h.after ?? null, unit)}
              </span>
              <span className="text-muted-foreground normal-case tracking-normal">
                by {h.actor_email ?? h.actor_user_id ?? "system"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function RiskKnobsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "risk-knobs"] as const;
  const historyKey = ["admin", "risk-knobs", "history"] as const;

  const { data, isLoading, error } = useQuery<RiskKnobState>({
    queryKey,
    queryFn: () =>
      apiFetch<RiskKnobState>("/api/admin/risk-knobs", { getToken: tokenFn }),
    enabled: !!isSignedIn,
  });

  const historyQuery = useQuery<RiskKnobHistoryResponse>({
    queryKey: historyKey,
    queryFn: () =>
      apiFetch<RiskKnobHistoryResponse>(
        "/api/admin/risk-knobs/history?per_knob_limit=10",
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
  });

  // Local draft of slider positions so the UI stays responsive while
  // the operator drags. Only flushed to the server on "Apply".
  const [draft, setDraft] = useState<Record<string, number>>({});
  const draftKeys = Object.keys(draft);

  const dirty = useMemo(() => {
    if (!data) return [] as string[];
    return draftKeys.filter((k) => {
      const knob = data.knobs.find((n) => n.key === k);
      if (!knob) return false;
      const eff = knob.effective_value ?? 0;
      return Math.abs(eff - draft[k]) > 1e-9;
    });
  }, [draft, data, draftKeys]);

  const apply = useMutation({
    mutationFn: () =>
      apiFetch<RiskKnobState>("/api/admin/risk-knobs", {
        method: "POST",
        body: { overrides: Object.fromEntries(dirty.map((k) => [k, draft[k]])) },
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      queryClient.invalidateQueries({ queryKey: historyKey });
      setDraft({});
    },
  });

  const reset = useMutation({
    mutationFn: (key: string) =>
      apiFetch<RiskKnobState>("/api/admin/risk-knobs", {
        method: "POST",
        body: { overrides: { [key]: null } },
        getToken: tokenFn,
      }),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKey, next);
      queryClient.invalidateQueries({ queryKey: historyKey });
      setDraft((d) => {
        const out = { ...d };
        return out;
      });
    },
  });

  return (
    <main
      className="grid grid-cols-1 gap-4"
      data-testid="panel-risk-knobs"
    >
      <div className="border border-border bg-card p-4">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div>
            <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
              Live risk knobs
            </h2>
            <p className="text-xs text-muted-foreground mt-1 normal-case tracking-normal">
              Per-knob overrides applied on top of the active aggression mode.
              Changes take effect on the next orchestrator cycle and are
              recorded in the admin audit log. Clear an override to fall back
              to the mode default.
            </p>
          </div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground whitespace-nowrap">
            Mode: {data?.aggression_mode ?? "—"}
          </div>
        </div>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-muted/30" />
        ) : error ? (
          <div className="text-loss text-xs">Failed to load risk knobs.</div>
        ) : data ? (
          <div className="space-y-3">
            {data.knobs.map((knob) => {
              const draftVal =
                draft[knob.key] ?? knob.effective_value ?? knob.minimum;
              const isDirty = dirty.includes(knob.key);
              return (
                <div
                  key={knob.key}
                  className="border border-border p-3 bg-background"
                  data-testid={`row-knob-${knob.key}`}
                >
                  <div className="flex items-baseline justify-between gap-4">
                    <div>
                      <div className="text-xs uppercase tracking-widest text-foreground">
                        {knob.label}
                      </div>
                      <div className="text-[10px] text-muted-foreground normal-case tracking-normal">
                        {knob.description}
                      </div>
                    </div>
                    <div className="text-right text-[10px] uppercase tracking-widest text-muted-foreground">
                      <div>
                        Default {formatKnob(knob.default_value, knob.unit)}
                      </div>
                      <div>
                        Override{" "}
                        <span
                          className={
                            knob.override_value != null
                              ? "text-primary"
                              : "text-muted-foreground"
                          }
                        >
                          {knob.override_value != null
                            ? formatKnob(knob.override_value, knob.unit)
                            : "none"}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 mt-3">
                    <input
                      type="range"
                      min={knob.minimum}
                      max={knob.maximum}
                      step={knob.step}
                      value={draftVal}
                      onChange={(e) =>
                        setDraft((d) => ({
                          ...d,
                          [knob.key]: Number(e.target.value),
                        }))
                      }
                      className="flex-1 accent-primary"
                      data-testid={`slider-knob-${knob.key}`}
                    />
                    <input
                      type="number"
                      min={knob.minimum}
                      max={knob.maximum}
                      step={knob.step}
                      value={draftVal}
                      onChange={(e) =>
                        setDraft((d) => ({
                          ...d,
                          [knob.key]: Number(e.target.value),
                        }))
                      }
                      className="w-24 bg-input border border-border px-2 py-1 text-xs"
                      data-testid={`input-knob-${knob.key}`}
                    />
                    <span
                      className={`text-xs uppercase tracking-widest ${
                        isDirty ? "text-primary" : "text-muted-foreground"
                      }`}
                    >
                      {formatKnob(draftVal, knob.unit)}
                    </span>
                    {knob.override_value != null ? (
                      <button
                        type="button"
                        disabled={reset.isPending}
                        onClick={() => reset.mutate(knob.key)}
                        data-testid={`button-knob-reset-${knob.key}`}
                        className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-destructive hover:border-destructive"
                      >
                        Clear
                      </button>
                    ) : null}
                  </div>
                  <KnobHistoryList
                    knobKey={knob.key}
                    unit={knob.unit}
                    entries={historyQuery.data?.history?.[knob.key] ?? []}
                    loading={historyQuery.isLoading}
                  />
                </div>
              );
            })}
            <div className="flex items-center gap-3 pt-2">
              <button
                type="button"
                disabled={dirty.length === 0 || apply.isPending}
                onClick={() => apply.mutate()}
                data-testid="button-knobs-apply"
                className={`border px-4 py-2 text-xs uppercase tracking-widest ${
                  dirty.length === 0
                    ? "border-border text-muted-foreground"
                    : "border-primary text-primary hover:bg-primary/10"
                }`}
              >
                {apply.isPending
                  ? "Applying…"
                  : `Apply ${dirty.length} change${dirty.length === 1 ? "" : "s"}`}
              </button>
              {dirty.length > 0 ? (
                <button
                  type="button"
                  disabled={apply.isPending}
                  onClick={() => setDraft({})}
                  data-testid="button-knobs-discard"
                  className="border border-border px-3 py-2 text-xs uppercase tracking-widest text-muted-foreground hover:text-foreground"
                >
                  Discard
                </button>
              ) : null}
              {apply.isError ? (
                <span className="text-loss text-xs">
                  {(apply.error as Error)?.message ?? "Failed to apply."}
                </span>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Trigger cycle panel
// ---------------------------------------------------------------------------

type CycleResult = {
  cycle_id: string;
  vertical: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  picks_evaluated: number;
  trades_placed: number;
  fresh_debates: number;
  errors: string[];
  trace: Array<{
    cycle_id?: string;
    stage?: string;
    event?: string;
    ts?: string;
    [k: string]: unknown;
  }>;
};

type TraceEvent = CycleResult["trace"][number];

export function TriggerCyclePanel() {
  const { getToken } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [vertical, setVertical] = useState("sports");
  const [last, setLast] = useState<CycleResult | null>(null);
  const [liveTrace, setLiveTrace] = useState<TraceEvent[]>([]);
  // We don't know the cycle_id ahead of time, so capture every event
  // received while the trigger is in-flight. Once the response lands
  // we can filter to that exact cycle_id and replace with the
  // authoritative trace from the server's ring buffer.
  const isStreamingRef = useRef(false);
  const seenSeqRef = useRef<Set<number>>(new Set());

  const trigger = useMutation({
    mutationFn: () =>
      apiFetch<CycleResult>("/api/admin/orchestrator/run-cycle", {
        method: "POST",
        body: { vertical },
        getToken: tokenFn,
      }),
    onMutate: () => {
      setLast(null);
      setLiveTrace([]);
      seenSeqRef.current = new Set();
      isStreamingRef.current = true;
    },
    onSuccess: (res) => {
      isStreamingRef.current = false;
      setLast(res);
    },
    onError: () => {
      isStreamingRef.current = false;
    },
  });

  usePipelineSubscription((event) => {
    if (!isStreamingRef.current) return;
    const seqRaw = event["_seq"];
    const seq = typeof seqRaw === "number" ? seqRaw : null;
    if (seq != null) {
      if (seenSeqRef.current.has(seq)) return;
      seenSeqRef.current.add(seq);
    }
    setLiveTrace((prev) => [...prev, event as TraceEvent]);
  });

  // If a result lands, drop any live events that don't belong to its
  // cycle (e.g. a background scheduler cycle that fired concurrently).
  useEffect(() => {
    if (!last) return;
    setLiveTrace((prev) =>
      prev.filter((ev) => !ev.cycle_id || ev.cycle_id === last.cycle_id),
    );
  }, [last]);

  const streaming = trigger.isPending;
  // Prefer the authoritative server trace once the cycle returns, but
  // fall back to streamed events that arrived after it (rare — the
  // ring buffer is the source of truth).
  const displayTrace: TraceEvent[] = useMemo(() => {
    if (!last) return liveTrace;
    if (liveTrace.length <= last.trace.length) return last.trace;
    const known = new Set<number>();
    for (const ev of last.trace) {
      const s = (ev as Record<string, unknown>)["_seq"];
      if (typeof s === "number") known.add(s);
    }
    const extras = liveTrace.filter((ev) => {
      const s = (ev as Record<string, unknown>)["_seq"];
      return typeof s === "number" ? !known.has(s) : true;
    });
    return [...last.trace, ...extras];
  }, [last, liveTrace]);

  return (
    <main className="grid grid-cols-1 gap-4" data-testid="panel-trigger-cycle">
      <div className="border border-border bg-card p-4">
        <h2 className="text-xs uppercase tracking-widest text-muted-foreground mb-3">
          Trigger one orchestrator cycle
        </h2>
        <p className="text-xs text-muted-foreground mb-4 normal-case tracking-normal">
          Runs a single out-of-band cycle for the chosen vertical and shows
          the pipeline trace it produced. Refused (503) until the readiness
          gate has flipped.
        </p>
        <div className="flex items-center gap-2 mb-3">
          <label className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Vertical
          </label>
          <input
            value={vertical}
            onChange={(e) => setVertical(e.target.value)}
            data-testid="input-cycle-vertical"
            className="bg-input border border-border px-2 py-1 text-xs w-40"
          />
          <button
            type="button"
            disabled={trigger.isPending || !vertical.trim()}
            onClick={() => trigger.mutate()}
            data-testid="button-trigger-cycle"
            className={`border px-4 py-1.5 text-xs uppercase tracking-widest ${
              trigger.isPending
                ? "border-border text-muted-foreground"
                : "border-primary text-primary hover:bg-primary/10"
            }`}
          >
            {trigger.isPending ? "Running…" : "Trigger cycle"}
          </button>
          {trigger.isError ? (
            <span className="text-loss text-xs">
              {(trigger.error as Error)?.message ?? "Cycle failed."}
            </span>
          ) : null}
        </div>
        {streaming || last ? (
          <div
            className="border border-border bg-background p-3"
            data-testid="block-cycle-result"
          >
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
              <ThresholdStat
                label="CYCLE_ID"
                value={last ? last.cycle_id.slice(0, 12) : "…"}
              />
              <ThresholdStat
                label="VERTICAL"
                value={last ? last.vertical : vertical}
              />
              <ThresholdStat
                label="DURATION"
                value={
                  last && last.duration_seconds != null
                    ? `${last.duration_seconds.toFixed(2)}s`
                    : streaming
                    ? "running…"
                    : "—"
                }
              />
              <ThresholdStat
                label="PICKS"
                value={last ? String(last.picks_evaluated) : "—"}
              />
              <ThresholdStat
                label="TRADES"
                value={last ? String(last.trades_placed) : "—"}
              />
            </div>
            {last && last.errors.length > 0 ? (
              <div className="mt-3 text-xs text-loss">
                {last.errors.length} error(s):
                <ul className="list-disc list-inside">
                  {last.errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            <div className="mt-4">
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2 flex items-center gap-2">
                <span>
                  Pipeline trace ({displayTrace.length} events)
                </span>
                {streaming ? (
                  <span
                    className="inline-flex items-center gap-1 text-primary normal-case tracking-normal"
                    data-testid="text-cycle-streaming"
                  >
                    <span className="inline-block h-1.5 w-1.5 rounded-full bg-primary animate-pulse" />
                    streaming live
                  </span>
                ) : null}
              </div>
              <div
                className="max-h-96 overflow-auto border border-border bg-muted/10"
                data-testid="block-cycle-trace"
              >
                <table className="w-full text-[11px]">
                  <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest text-[10px]">
                    <tr>
                      <th className="text-left p-2">Stage</th>
                      <th className="text-left p-2">Event</th>
                      <th className="text-left p-2">Time</th>
                      <th className="text-left p-2">Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {displayTrace.length === 0 ? (
                      <tr>
                        <td
                          colSpan={4}
                          className="p-3 text-center text-muted-foreground"
                        >
                          {streaming
                            ? "Waiting for the first event…"
                            : "No trace events captured."}
                        </td>
                      </tr>
                    ) : (
                      displayTrace.map((ev, i) => {
                        const detail = { ...ev };
                        delete (detail as Record<string, unknown>).stage;
                        delete (detail as Record<string, unknown>).event;
                        delete (detail as Record<string, unknown>).ts;
                        delete (detail as Record<string, unknown>).cycle_id;
                        delete (detail as Record<string, unknown>)._seq;
                        return (
                          <tr
                            key={i}
                            className="border-t border-border/50 align-top"
                          >
                            <td className="p-2 text-foreground">
                              {String(ev.stage ?? "")}
                            </td>
                            <td className="p-2 text-primary">
                              {String(ev.event ?? "")}
                            </td>
                            <td className="p-2 text-muted-foreground">
                              {String(ev.ts ?? "")}
                            </td>
                            <td className="p-2 text-muted-foreground font-mono break-all">
                              {Object.keys(detail).length > 0
                                ? JSON.stringify(detail)
                                : ""}
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Models browser panel
// ---------------------------------------------------------------------------

type ModelRow = {
  name: string;
  version: string;
  sha256: string;
  size: number;
  mtime: number;
  loaded: boolean;
  last_loaded_at: string | null;
  smoke_load_seconds: number | null;
  is_active: boolean;
  is_override: boolean;
};

type ModelsPayload = {
  items: ModelRow[];
  ready: boolean;
  in_progress: boolean;
  active_predictor: { name?: string; version?: string } | null;
  active_override: string | null;
  manifest: Record<string, unknown> | null;
};

function fmtSize(bytes: number): string {
  if (!bytes) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toISOString().replace("T", " ").slice(0, 19);
}

export function ModelsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "models"] as const;

  const { data, isLoading, error } = useQuery<ModelsPayload>({
    queryKey,
    queryFn: () =>
      apiFetch<ModelsPayload>("/api/admin/models", { getToken: tokenFn }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const setActive = useMutation({
    mutationFn: (name: string | null) =>
      apiFetch<ModelsPayload>("/api/admin/models/active", {
        method: "POST",
        body: { name },
        getToken: tokenFn,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  return (
    <main className="grid grid-cols-1 gap-4" data-testid="panel-models">
      <div className="border border-border bg-card p-4">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div>
            <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
              Predictor catalog
            </h2>
            <p className="text-xs text-muted-foreground mt-1 normal-case tracking-normal">
              Versioned predictor artifacts in storage. Click "Make active" to
              pin one as the predictor used by the next /predict call. Pinning
              survives a restart and is recorded in the audit log.
            </p>
          </div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground text-right">
            <div>
              Active:{" "}
              <span className="text-primary">
                {data?.active_predictor?.name ?? "—"}
              </span>
            </div>
            <div>
              Override:{" "}
              <span
                className={
                  data?.active_override ? "text-primary" : "text-muted-foreground"
                }
              >
                {data?.active_override ?? "none"}
              </span>
            </div>
            {data?.active_override ? (
              <button
                type="button"
                disabled={setActive.isPending}
                onClick={() => setActive.mutate(null)}
                data-testid="button-clear-active-override"
                className="mt-1 border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-destructive hover:border-destructive"
              >
                Clear override
              </button>
            ) : null}
          </div>
        </div>
        {isLoading ? (
          <div className="h-32 animate-pulse bg-muted/30" />
        ) : error ? (
          <div className="text-loss text-xs">Failed to load model catalog.</div>
        ) : data ? (
          <div className="border border-border bg-background overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
                <tr>
                  <th className="text-left p-2">Name</th>
                  <th className="text-left p-2">Version</th>
                  <th className="text-left p-2">SHA-256</th>
                  <th className="text-right p-2">Size</th>
                  <th className="text-left p-2">Updated</th>
                  <th className="text-left p-2">Last loaded</th>
                  <th className="text-right p-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {data.items.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-muted-foreground">
                      No predictors in catalog.
                    </td>
                  </tr>
                ) : (
                  data.items.map((m) => (
                    <tr
                      key={m.name}
                      className="border-t border-border/50 align-top"
                      data-testid={`row-model-${m.name}`}
                    >
                      <td className="p-2 text-foreground break-all">
                        {m.name}
                        {m.is_active ? (
                          <span className="ml-2 border border-primary text-primary px-1.5 py-0.5 text-[10px] uppercase tracking-widest">
                            Active
                          </span>
                        ) : null}
                        {m.loaded && !m.is_active ? (
                          <span className="ml-2 border border-success text-success px-1.5 py-0.5 text-[10px] uppercase tracking-widest">
                            Loaded
                          </span>
                        ) : null}
                      </td>
                      <td className="p-2 text-muted-foreground">{m.version || "—"}</td>
                      <td className="p-2 text-muted-foreground font-mono">
                        {m.sha256 ? `${m.sha256.slice(0, 12)}…` : "—"}
                      </td>
                      <td className="p-2 text-muted-foreground text-right">
                        {fmtSize(m.size)}
                      </td>
                      <td className="p-2 text-muted-foreground">{fmtTime(m.mtime)}</td>
                      <td className="p-2 text-muted-foreground">
                        {m.last_loaded_at ?? "—"}
                      </td>
                      <td className="p-2 text-right">
                        {m.is_override ? (
                          <span className="text-[10px] uppercase tracking-widest text-primary">
                            Pinned
                          </span>
                        ) : (
                          <button
                            type="button"
                            disabled={setActive.isPending}
                            onClick={() => setActive.mutate(m.name)}
                            data-testid={`button-make-active-${m.name}`}
                            className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest hover:text-primary hover:border-primary"
                          >
                            Make active
                          </button>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        ) : null}
        {setActive.isError ? (
          <div className="text-loss text-xs mt-2">
            {(setActive.error as Error)?.message ?? "Failed to set active model."}
          </div>
        ) : null}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// GitHub mirror status (task #172) — surface whether the recovery mirror is
// configured, the last successfully mirrored model version, the timestamp +
// outcome of the most recent push, and a "Push to mirror now" button.
// ---------------------------------------------------------------------------

type MirrorLastResult = {
  ok: boolean;
  skipped: boolean;
  reason: string | null;
  version: string | null;
  files_pushed: string[];
  elapsed_seconds: number;
  started_at: number | null;
  completed_at: number | null;
};

type MirrorPersistedState = {
  last_version?: string;
  last_mirrored_at?: number;
  repo?: string;
  branch?: string;
  files_pushed?: string[];
};

type MirrorStatusPayload = {
  enabled: boolean;
  config: { repo?: string; branch?: string };
  last_result: MirrorLastResult | null;
  persisted_state: MirrorPersistedState;
};

function fmtMirrorTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toISOString().replace("T", " ").slice(0, 19);
}

export function GithubMirrorPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "models", "mirror", "status"] as const;

  const { data, isLoading, error } = useQuery<MirrorStatusPayload>({
    queryKey,
    queryFn: () =>
      apiFetch<MirrorStatusPayload>("/api/admin/models/mirror/status", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const pushNow = useMutation({
    mutationFn: () =>
      apiFetch<MirrorLastResult>("/api/admin/models/mirror/push", {
        method: "POST",
        getToken: tokenFn,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  if (isLoading) {
    return (
      <div className="h-24 animate-pulse bg-muted/30" data-testid="mirror-loading" />
    );
  }
  if (error || !data) {
    return (
      <div className="text-loss text-xs" data-testid="mirror-error">
        Failed to load mirror status.
      </div>
    );
  }

  const enabled = data.enabled;
  const last = data.last_result;
  const persisted = data.persisted_state ?? {};

  const stateLabel = !enabled
    ? "DISABLED"
    : last && !last.ok && !last.skipped
      ? "FAILED"
      : last && last.ok
        ? "OK"
        : persisted.last_version
          ? "OK"
          : "PENDING";
  const stateClass = !enabled
    ? "border-border text-muted-foreground"
    : stateLabel === "FAILED"
      ? "border-loss/60 bg-loss/10 text-loss"
      : stateLabel === "PENDING"
        ? "border-accent-warning/60 text-accent-warning"
        : "border-accent-cyan/40 text-accent-cyan";

  const lastVersion = last?.version ?? persisted.last_version ?? null;
  const lastTimestamp =
    last?.completed_at ?? persisted.last_mirrored_at ?? null;
  const repo = data.config.repo ?? persisted.repo ?? null;
  const branch = data.config.branch ?? persisted.branch ?? null;
  const lastPushed = last?.files_pushed ?? [];

  return (
    <div className="space-y-3" data-testid="panel-github-mirror">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-[11px] text-muted-foreground leading-snug normal-case tracking-normal">
          Every successful <code>/api/models/reload</code> mirrors the active
          model artifact + manifest to a private GitHub repo so we can recover
          from a Supabase outage. Push is best-effort and runs in the
          background; use the button below to force a re-sync.
        </p>
        <span
          className={`text-[10px] uppercase border px-2 py-0.5 ${stateClass}`}
          data-testid="mirror-state"
        >
          {stateLabel}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 text-[11px] md:grid-cols-4">
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Repo
          </div>
          <div
            className="font-mono text-xs break-all text-foreground"
            data-testid="mirror-repo"
          >
            {repo ?? "—"}
          </div>
          {branch && (
            <div className="text-[10px] text-muted-foreground">
              branch {branch}
            </div>
          )}
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Last mirrored version
          </div>
          <div
            className="font-mono text-xs break-all text-foreground"
            data-testid="mirror-last-version"
          >
            {lastVersion ?? "—"}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Last push
          </div>
          <div
            className="font-mono text-xs text-foreground"
            data-testid="mirror-last-push-time"
          >
            {fmtMirrorTime(lastTimestamp)}
          </div>
          {last && (
            <div className="text-[10px] text-muted-foreground">
              took {last.elapsed_seconds.toFixed(2)}s
            </div>
          )}
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Last result
          </div>
          <div
            className={`font-mono text-xs ${
              last && !last.ok && !last.skipped
                ? "text-loss"
                : last && last.skipped
                  ? "text-muted-foreground"
                  : last && last.ok
                    ? "text-accent-cyan"
                    : "text-muted-foreground"
            }`}
            data-testid="mirror-last-result"
          >
            {last
              ? last.ok
                ? last.skipped
                  ? "skipped"
                  : `pushed ${last.files_pushed.length} file(s)`
                : "failed"
              : persisted.last_version
                ? "no push since restart"
                : "never"}
          </div>
          {last?.reason && (
            <div
              className="text-[10px] text-muted-foreground normal-case tracking-normal break-words"
              data-testid="mirror-last-reason"
            >
              {last.reason}
            </div>
          )}
        </div>
      </div>

      {lastPushed.length > 0 && (
        <details
          className="border border-border bg-background/50 p-2 text-[11px]"
          data-testid="mirror-files-pushed"
        >
          <summary className="cursor-pointer text-muted-foreground uppercase tracking-wider text-[10px]">
            Files pushed in last run ({lastPushed.length})
          </summary>
          <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-foreground">
            {lastPushed.map((p) => (
              <li key={p} className="break-all">
                {p}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
        <button
          type="button"
          disabled={!enabled || pushNow.isPending}
          onClick={() => pushNow.mutate()}
          data-testid="button-mirror-push-now"
          className="border border-border px-3 py-1.5 text-[10px] uppercase tracking-widest hover:text-primary hover:border-primary disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {pushNow.isPending ? "Pushing…" : "Push to mirror now"}
        </button>
        {!enabled && (
          <span className="text-[11px] text-muted-foreground normal-case tracking-normal">
            Set <code>GITHUB_MIRROR_REPO</code> and <code>GITHUB_TOKEN</code>{" "}
            to enable.
          </span>
        )}
        {pushNow.isError && (
          <span
            className="text-loss text-[11px] normal-case tracking-normal"
            data-testid="mirror-push-error"
          >
            {(pushNow.error as Error)?.message ?? "Mirror push failed."}
          </span>
        )}
        {pushNow.isSuccess && pushNow.data && (
          <span
            className={`text-[11px] normal-case tracking-normal ${
              pushNow.data.ok ? "text-accent-cyan" : "text-loss"
            }`}
            data-testid="mirror-push-result"
          >
            {pushNow.data.ok
              ? pushNow.data.skipped
                ? `Skipped: ${pushNow.data.reason ?? "no changes"}`
                : `Pushed ${pushNow.data.files_pushed.length} file(s) in ${pushNow.data.elapsed_seconds.toFixed(2)}s.`
              : `Failed: ${pushNow.data.reason ?? "unknown error"}`}
          </span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Personas tab — per-vertical persona enable/disable (Task #194)
// ---------------------------------------------------------------------------

type PersonaToggleRow = {
  id: string;
  display: string;
  provider: string | null;
  model_id: string | null;
  enabled: boolean;
};

type PersonaToggleVertical = {
  vertical: string;
  vertical_label: string;
  persona_profile: string | null;
  profile_description: string | null;
  personas: PersonaToggleRow[];
  orphaned?: boolean;
};

type PersonaToggleSummary = {
  items: PersonaToggleVertical[];
  available_profiles: { name: string; description: string; personas: string[] }[];
};

export function PersonasPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "persona-toggles"] as const;

  const { data, isLoading, error } = useQuery<PersonaToggleSummary>({
    queryKey,
    queryFn: () =>
      apiFetch<PersonaToggleSummary>("/api/admin/persona-toggles", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
  });

  const mutation = useMutation({
    mutationFn: (vars: {
      vertical: string;
      persona: string;
      enabled: boolean;
    }) =>
      apiFetch<unknown>("/api/admin/persona-toggles", {
        method: "POST",
        body: vars,
        getToken: tokenFn,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  return (
    <main
      className="grid grid-cols-1 gap-6"
      data-testid="panel-persona-toggles"
    >
      <section className="border border-border bg-card p-4">
        <h2 className="text-xs uppercase tracking-widest text-muted-foreground mb-3">
          Per-vertical persona profiles
        </h2>
        <p className="text-xs text-muted-foreground normal-case tracking-normal mb-4">
          Each vertical runs its own swarm profile. Toggle individual personas
          off when you want the swarm to ignore one in that domain — the change
          applies on the next debate. Disabling every persona in a vertical
          falls back to the full profile so the swarm never silently drops to
          a single LLM call. The moderator and gatekeeper layer is unaffected.
        </p>
        {isLoading ? (
          <div className="h-24 animate-pulse bg-muted/30" />
        ) : error ? (
          <div className="text-loss text-xs">
            Failed to load persona toggles.
          </div>
        ) : data ? (
          <div className="space-y-4">
            {data.items.map((v) => (
              <div
                key={v.vertical}
                className="border border-border"
                data-testid={`persona-vertical-${v.vertical}`}
              >
                <header className="flex flex-wrap items-baseline gap-2 px-3 py-2 bg-muted/20">
                  <h3 className="text-xs uppercase tracking-widest">
                    {v.vertical_label}
                  </h3>
                  <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    profile: {v.persona_profile ?? "—"}
                    {v.orphaned ? " (orphaned override)" : ""}
                  </span>
                  {v.profile_description ? (
                    <span className="text-[11px] text-muted-foreground normal-case tracking-normal ml-auto">
                      {v.profile_description}
                    </span>
                  ) : null}
                </header>
                <table className="w-full text-xs">
                  <thead className="bg-muted/10 text-muted-foreground uppercase tracking-widest">
                    <tr>
                      <th className="text-left p-2">Persona</th>
                      <th className="text-left p-2">Provider / model</th>
                      <th className="text-right p-2">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {v.personas.map((p) => (
                      <tr key={p.id} className="border-t border-border">
                        <td className="p-2">
                          <div>{p.display}</div>
                          <div className="text-[10px] text-muted-foreground font-mono">
                            {p.id}
                          </div>
                        </td>
                        <td className="p-2 text-muted-foreground">
                          {p.provider ?? "—"} / {p.model_id ?? "—"}
                        </td>
                        <td className="p-2 text-right">
                          <button
                            type="button"
                            disabled={mutation.isPending}
                            onClick={() =>
                              mutation.mutate({
                                vertical: v.vertical,
                                persona: p.id,
                                enabled: !p.enabled,
                              })
                            }
                            data-testid={`button-toggle-${v.vertical}-${p.id}`}
                            className={`border px-3 py-1 text-[10px] uppercase tracking-widest ${
                              p.enabled
                                ? "border-primary text-primary"
                                : "border-border text-muted-foreground hover:text-foreground"
                            } ${mutation.isPending ? "opacity-50" : ""}`}
                          >
                            {p.enabled ? "Enabled" : "Disabled"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        ) : null}
        {mutation.isError ? (
          <div className="text-loss text-xs mt-2">
            {(mutation.error as Error)?.message ?? "Toggle failed."}
          </div>
        ) : null}
      </section>
    </main>
  );
}

export function VerticalVisibilityPanel() {
  const qc = useQueryClient();
  const overview = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 30_000,
      },
    },
  );
  const visibility = useAdminGetVerticalVisibility({
    query: {
      queryKey: getAdminGetVerticalVisibilityQueryKey(),
      staleTime: 30_000,
    },
  });

  const mutation = useAdminSetVerticalVisibility({
    mutation: {
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: getAdminGetVerticalVisibilityQueryKey() });
        qc.invalidateQueries({ queryKey: getGetVerticalsOverviewQueryKey() });
        qc.invalidateQueries({ queryKey: getGetVerticalVisibilityQueryKey() });
      },
    },
  });

  const all = overview.data?.verticals ?? [];
  const hidden = new Set(visibility.data?.hidden ?? []);
  const pinned = visibility.data?.pinned ?? [];

  function toggleHidden(name: string) {
    const next = new Set(hidden);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    mutation.mutate({ data: { hidden: Array.from(next), pinned } });
  }

  function togglePinned(name: string) {
    const next = pinned.includes(name)
      ? pinned.filter((p) => p !== name)
      : [...pinned, name];
    mutation.mutate({ data: { hidden: Array.from(hidden), pinned: next } });
  }

  return (
    <main className="border border-border bg-card p-4">
      <header className="mb-3 pb-2 border-b border-border">
        <h2 className="text-sm uppercase tracking-widest font-bold text-primary">
          Vertical visibility
        </h2>
        <p className="text-xs text-muted-foreground mt-1">
          Pinned verticals lead the dashboard strip in pin order. Hidden
          verticals are removed from the strip for non-admin operators
          (admins still see them in the picker, dimmed). Every change is
          written to <code>system_settings</code> and audited.
        </p>
      </header>

      {(overview.isLoading || visibility.isLoading) && (
        <div className="text-xs text-muted-foreground uppercase tracking-widest">
          Loading…
        </div>
      )}

      <table className="w-full text-xs">
        <thead className="text-muted-foreground uppercase tracking-widest">
          <tr>
            <th className="text-left p-2">Vertical</th>
            <th className="text-left p-2">Description</th>
            <th className="text-left p-2">Pinned</th>
            <th className="text-left p-2">Hidden</th>
          </tr>
        </thead>
        <tbody>
          {all.map((entry) => {
            const pinIdx = pinned.indexOf(entry.vertical);
            const isPinned = pinIdx >= 0;
            const isHidden = hidden.has(entry.vertical);
            return (
              <tr
                key={entry.vertical}
                className="border-t border-border"
                data-testid={`row-vertical-vis-${entry.vertical}`}
              >
                <td className="p-2 uppercase font-bold text-primary">{entry.vertical}</td>
                <td className="p-2 text-muted-foreground">{entry.description ?? "—"}</td>
                <td className="p-2">
                  <button
                    type="button"
                    onClick={() => togglePinned(entry.vertical)}
                    disabled={mutation.isPending}
                    className={`px-2 py-0.5 border text-[10px] uppercase tracking-widest ${
                      isPinned
                        ? "border-primary text-primary"
                        : "border-border text-muted-foreground hover:text-primary"
                    }`}
                    data-testid={`button-pin-${entry.vertical}`}
                  >
                    {isPinned ? `★ ${pinIdx + 1}` : "Pin"}
                  </button>
                </td>
                <td className="p-2">
                  <button
                    type="button"
                    onClick={() => toggleHidden(entry.vertical)}
                    disabled={mutation.isPending}
                    className={`px-2 py-0.5 border text-[10px] uppercase tracking-widest ${
                      isHidden
                        ? "border-destructive text-destructive"
                        : "border-border text-muted-foreground hover:text-destructive"
                    }`}
                    data-testid={`button-hide-${entry.vertical}`}
                  >
                    {isHidden ? "Hidden" : "Hide"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {mutation.isError && (
        <div className="mt-3 text-xs text-destructive">
          Update failed. {(mutation.error as Error)?.message ?? ""}
        </div>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// RevenueCat (mobile IAP) webhook health tile
//
// Surfaces the last 24h of webhook deliveries split by status. A silent
// outage means paying mobile customers stop being upgraded, so we want
// the operator to spot a spike or a long stretch of silence at a
// glance. Backed by /api/admin/rc-webhook/stats.
// ---------------------------------------------------------------------------

type RcWebhookStats = {
  hours: number;
  totals: Record<string, number>;
  hourly: { hour: string | null; status: string; count: number }[];
  last_event_at: string | null;
  last_5xx_at: string | null;
  alert: {
    failure_threshold_pct: number;
    failures_last_hour: number;
    failure_rate_pct: number;
    silence_threshold_seconds: number;
    business_hours_utc: number[];
    in_business_hours: boolean;
  };
};

const RC_STATUSES = ["ok", "duplicate", "http_4xx", "http_5xx"] as const;
const RC_STATUS_LABEL: Record<(typeof RC_STATUSES)[number], string> = {
  ok: "OK",
  duplicate: "Duplicate",
  http_4xx: "4xx",
  http_5xx: "5xx",
};
const RC_STATUS_COLOR: Record<(typeof RC_STATUSES)[number], string> = {
  ok: "bg-emerald-500/70",
  duplicate: "bg-sky-500/60",
  http_4xx: "bg-amber-500/70",
  http_5xx: "bg-rose-500/80",
};

function _fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

type RcFailedEvent = {
  event_id: string;
  event_type: string | null;
  last_failed_at: string | null;
  attempts: number;
  last_error: string | null;
  raw_available: boolean;
  user_id: string | null;
  app_user_id: string | null;
};

type RcRetryOutcome = {
  event_id: string;
  status: "ok" | "failed" | "missing";
  message: string;
  at: number;
};

export function RcWebhookPanel() {
  const { getToken, isSignedIn } = useAuth();
  const qc = useQueryClient();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const query = useQuery<RcWebhookStats>({
    queryKey: ["admin", "rc-webhook-stats"],
    queryFn: () =>
      apiFetch<RcWebhookStats>("/api/admin/rc-webhook/stats?hours=24", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });

  const failedQuery = useQuery<{ items: RcFailedEvent[] }>({
    queryKey: ["admin", "rc-webhook-failed-events"],
    queryFn: () =>
      apiFetch<{ items: RcFailedEvent[] }>(
        "/api/admin/rc-webhook/failed-events?limit=20",
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });

  const [retryOutcome, setRetryOutcome] = useState<RcRetryOutcome | null>(null);

  const retryMutation = useMutation({
    mutationFn: async (eventId: string) =>
      apiFetch<{ status: string; event_id: string; event_type?: string | null }>(
        `/api/admin/rc-webhook/retry/${encodeURIComponent(eventId)}`,
        { method: "POST", getToken: tokenFn },
      ),
    onSuccess: (res, eventId) => {
      setRetryOutcome({
        event_id: eventId,
        status: "ok",
        message: `Replayed ${res.event_type ?? "event"} successfully.`,
        at: Date.now(),
      });
      qc.invalidateQueries({ queryKey: ["admin", "rc-webhook-failed-events"] });
      qc.invalidateQueries({ queryKey: ["admin", "rc-webhook-stats"] });
    },
    onError: (err: unknown, eventId) => {
      const msg = (err as Error)?.message ?? "retry failed";
      setRetryOutcome({
        event_id: eventId,
        status: msg.includes("404") ? "missing" : "failed",
        message: msg,
        at: Date.now(),
      });
      qc.invalidateQueries({ queryKey: ["admin", "rc-webhook-failed-events"] });
      qc.invalidateQueries({ queryKey: ["admin", "rc-webhook-stats"] });
    },
  });

  const failedEvents = failedQuery.data?.items ?? [];
  const data = query.data;

  // Bucket the hourly rows by hour key for a 24-column stacked bar.
  const buckets = useMemo(() => {
    if (!data) return [] as { hour: string; counts: Record<string, number>; total: number }[];
    const byHour = new Map<string, Record<string, number>>();
    for (const r of data.hourly) {
      if (!r.hour) continue;
      const slot = byHour.get(r.hour) ?? {};
      slot[r.status] = (slot[r.status] ?? 0) + r.count;
      byHour.set(r.hour, slot);
    }
    const sorted = Array.from(byHour.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([hour, counts]) => {
        const total = RC_STATUSES.reduce((s, k) => s + (counts[k] ?? 0), 0);
        return { hour, counts, total };
      });
    return sorted.slice(-24);
  }, [data]);

  const maxBucket = useMemo(
    () => buckets.reduce((m, b) => Math.max(m, b.total), 0) || 1,
    [buckets],
  );

  // Health summary line shown above the chart.
  const healthBadge = useMemo(() => {
    if (!data) return { label: "Loading", tone: "text-muted-foreground" };
    const failPct = data.alert.failure_rate_pct;
    const threshold = data.alert.failure_threshold_pct;
    const lastEventSec = data.last_event_at
      ? (Date.now() - Date.parse(data.last_event_at)) / 1000
      : Infinity;
    if (failPct >= threshold && data.alert.failures_last_hour > 0) {
      return {
        label: `5xx rate ${failPct.toFixed(0)}% — alert tripped`,
        tone: "text-destructive",
      };
    }
    if (
      data.alert.in_business_hours &&
      lastEventSec > data.alert.silence_threshold_seconds
    ) {
      return {
        label: "Silent during business hours — alert tripped",
        tone: "text-destructive",
      };
    }
    if (data.totals.http_5xx > 0) {
      return {
        label: `${data.totals.http_5xx} 5xx in 24h`,
        tone: "text-amber-500",
      };
    }
    return { label: "Healthy", tone: "text-emerald-500" };
  }, [data]);

  return (
    <main className="space-y-3" data-testid="panel-rc-webhook">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-xs uppercase tracking-widest text-muted-foreground">
            Mobile IAP webhook (RevenueCat) · 24h
          </h2>
          <p className="text-[11px] text-muted-foreground mt-1 normal-case tracking-normal">
            Counts every delivery the api-server has seen — including
            duplicates and rejects — so a silent dashboard pause or a
            spike in 5xx errors is visible without grepping logs.
          </p>
        </div>
        <div
          className={`text-[11px] uppercase tracking-widest ${healthBadge.tone}`}
          data-testid="text-rc-webhook-health"
        >
          {healthBadge.label}
        </div>
      </div>

      {query.isLoading && !data ? (
        <div className="h-32 animate-pulse bg-muted/30" />
      ) : query.isError ? (
        <div className="text-xs text-destructive">
          Failed to load webhook stats. {(query.error as Error)?.message ?? ""}
        </div>
      ) : data ? (
        <>
          <div
            className="grid grid-cols-2 gap-2 sm:grid-cols-4"
            data-testid="grid-rc-webhook-totals"
          >
            {RC_STATUSES.map((s) => (
              <div
                key={s}
                className="border border-border bg-card p-2"
                data-testid={`tile-rc-${s}`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-block h-2 w-2 rounded-sm ${RC_STATUS_COLOR[s]}`}
                  />
                  <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    {RC_STATUS_LABEL[s]}
                  </span>
                </div>
                <div className="mt-1 font-mono text-lg text-foreground">
                  {data.totals[s] ?? 0}
                </div>
              </div>
            ))}
          </div>

          <div className="border border-border bg-card p-2">
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
              Hourly deliveries
            </div>
            {buckets.length === 0 ? (
              <div className="text-[11px] text-muted-foreground py-6 text-center">
                No webhook deliveries in the last 24h.
              </div>
            ) : (
              <div
                className="flex h-24 items-end gap-1"
                data-testid="chart-rc-webhook-hourly"
              >
                {buckets.map((b) => {
                  const heightPct = (b.total / maxBucket) * 100;
                  return (
                    <div
                      key={b.hour}
                      className="flex-1 flex flex-col-reverse"
                      title={`${b.hour}\n${RC_STATUSES.map(
                        (s) => `${RC_STATUS_LABEL[s]}: ${b.counts[s] ?? 0}`,
                      ).join("\n")}`}
                      style={{ height: `${heightPct}%` }}
                    >
                      {RC_STATUSES.map((s) => {
                        const c = b.counts[s] ?? 0;
                        if (c === 0) return null;
                        const segPct = (c / b.total) * 100;
                        return (
                          <div
                            key={s}
                            className={RC_STATUS_COLOR[s]}
                            style={{ height: `${segPct}%` }}
                          />
                        );
                      })}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 gap-1 text-[11px] text-muted-foreground sm:grid-cols-2">
            <div>
              Last event:{" "}
              <span
                className="text-foreground"
                data-testid="text-rc-last-event"
              >
                {_fmtRelative(data.last_event_at)}
              </span>
            </div>
            <div>
              Last 5xx:{" "}
              <span className="text-foreground">
                {_fmtRelative(data.last_5xx_at)}
              </span>
            </div>
            <div>
              5xx alert at ≥{data.alert.failure_threshold_pct.toFixed(0)}%/hr ·
              now {data.alert.failure_rate_pct.toFixed(1)}%
            </div>
            <div>
              Silence alert ≥{Math.round(data.alert.silence_threshold_seconds / 60)}m
              {" "}({data.alert.in_business_hours ? "in business hrs" : "off-hours"})
            </div>
          </div>
        </>
      ) : null}

      <div
        className="border border-border bg-card p-2"
        data-testid="panel-rc-failed-events"
      >
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <div>
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              Stuck deliveries (last 7d)
            </div>
            <div className="text-[11px] text-muted-foreground mt-0.5 normal-case tracking-normal">
              Events whose latest webhook delivery returned a 5xx. Retry
              re-runs the stored payload through the same code path the
              live webhook uses. Each click is recorded as
              <code className="ml-1">rc_webhook_retry</code> in the admin
              audit log.
            </div>
          </div>
          {failedQuery.isFetching ? (
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
              Refreshing…
            </span>
          ) : null}
        </div>

        {retryOutcome ? (
          <div
            className={`mb-2 border px-2 py-1 text-[11px] ${
              retryOutcome.status === "ok"
                ? "border-emerald-500/40 text-emerald-500"
                : "border-destructive/50 text-destructive"
            }`}
            data-testid="text-rc-retry-outcome"
          >
            <span className="font-mono">{retryOutcome.event_id}</span>:{" "}
            {retryOutcome.message}
          </div>
        ) : null}

        {failedQuery.isLoading && !failedQuery.data ? (
          <div className="h-12 animate-pulse bg-muted/30" />
        ) : failedQuery.isError ? (
          <div className="text-xs text-destructive">
            Failed to load stuck deliveries.{" "}
            {(failedQuery.error as Error)?.message ?? ""}
          </div>
        ) : failedEvents.length === 0 ? (
          <div className="py-4 text-center text-[11px] text-muted-foreground">
            No stuck deliveries in the last 7 days.
          </div>
        ) : (
          <div
            className="overflow-x-auto"
            data-testid="table-rc-failed-events"
          >
            <table className="w-full text-[11px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="px-2 py-1 text-left font-normal uppercase tracking-widest">
                    Event
                  </th>
                  <th className="px-2 py-1 text-left font-normal uppercase tracking-widest">
                    Type
                  </th>
                  <th className="px-2 py-1 text-right font-normal uppercase tracking-widest">
                    Tries
                  </th>
                  <th className="px-2 py-1 text-left font-normal uppercase tracking-widest">
                    Last failure
                  </th>
                  <th className="px-2 py-1 text-left font-normal uppercase tracking-widest">
                    Last error
                  </th>
                  <th className="px-2 py-1 text-right font-normal uppercase tracking-widest">
                    Action
                  </th>
                </tr>
              </thead>
              <tbody>
                {failedEvents.map((row) => {
                  const busy =
                    retryMutation.isPending &&
                    (retryMutation.variables as string | undefined) ===
                      row.event_id;
                  return (
                    <tr
                      key={row.event_id}
                      className="border-b border-border/50 align-top"
                      data-testid={`row-rc-failed-${row.event_id}`}
                    >
                      <td className="px-2 py-1 font-mono text-foreground">
                        {row.event_id}
                      </td>
                      <td className="px-2 py-1 text-muted-foreground">
                        {row.event_type ?? "—"}
                      </td>
                      <td className="px-2 py-1 text-right font-mono">
                        {row.attempts}
                      </td>
                      <td className="px-2 py-1 text-muted-foreground">
                        {_fmtRelative(row.last_failed_at)}
                      </td>
                      <td className="px-2 py-1 text-muted-foreground">
                        <span
                          className="block max-w-[28rem] truncate"
                          title={row.last_error ?? ""}
                        >
                          {row.last_error ?? "—"}
                        </span>
                      </td>
                      <td className="px-2 py-1 text-right">
                        <button
                          type="button"
                          onClick={() => retryMutation.mutate(row.event_id)}
                          disabled={!row.raw_available || busy}
                          className="border border-border bg-background px-2 py-0.5 text-[11px] uppercase tracking-widest text-foreground disabled:cursor-not-allowed disabled:opacity-40 hover:bg-muted/40"
                          data-testid={`button-retry-${row.event_id}`}
                          title={
                            row.raw_available
                              ? "Re-run _apply_event with the stored payload"
                              : "No stored payload — cannot retry"
                          }
                        >
                          {busy ? "Retrying…" : "Retry"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}

// ---------------------------------------------------------------------------
// Per-vertical halt toggle (Task #271)
//
// Sits alongside Vertical visibility on /system → controls. A halt writes
// to system_settings (risk.vertical_halted.<name>), cancels every resting
// Kalshi order attached to the vertical, and is picked up by the risk-gate
// chain on the very next cycle (no restart required). Resume clears the
// row. Both actions audit-log.
// ---------------------------------------------------------------------------

type VerticalHaltsPayload = {
  halts: Record<
    string,
    {
      by_user_id?: string | null;
      by_email?: string | null;
      at?: string | null;
      reason?: string | null;
    }
  >;
};

type HaltCancelSummary = {
  scanned: number;
  matched: number;
  cancelled: number;
  failed: number;
};

export function VerticalHaltsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const qc = useQueryClient();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const overview = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 30_000,
      },
    },
  );

  const haltsQuery = useQuery<VerticalHaltsPayload>({
    queryKey: ["admin", "vertical-halts"],
    queryFn: () =>
      apiFetch<VerticalHaltsPayload>("/api/admin/verticals/halts", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });

  const [lastCancel, setLastCancel] = useState<
    { vertical: string; summary: HaltCancelSummary } | null
  >(null);
  const [pending, setPending] = useState<
    { vertical: string; action: "halt" | "resume"; reason: string } | null
  >(null);

  const haltMutation = useMutation({
    mutationFn: async (vars: { vertical: string; reason: string | null }) =>
      apiFetch<{ resting_orders: HaltCancelSummary; changed: boolean }>(
        `/api/admin/verticals/${encodeURIComponent(vars.vertical)}/halt`,
        {
          method: "POST",
          getToken: tokenFn,
          body: JSON.stringify({ reason: vars.reason }),
          headers: { "Content-Type": "application/json" },
        },
      ),
    onSuccess: (result, vars) => {
      setLastCancel({ vertical: vars.vertical, summary: result.resting_orders });
      setPending(null);
      qc.invalidateQueries({ queryKey: ["admin", "vertical-halts"] });
    },
  });

  const resumeMutation = useMutation({
    mutationFn: async (vars: { vertical: string; reason: string | null }) =>
      apiFetch<{ changed: boolean }>(
        `/api/admin/verticals/${encodeURIComponent(vars.vertical)}/resume`,
        {
          method: "POST",
          getToken: tokenFn,
          body: JSON.stringify({ reason: vars.reason }),
          headers: { "Content-Type": "application/json" },
        },
      ),
    onSuccess: () => {
      setLastCancel(null);
      setPending(null);
      qc.invalidateQueries({ queryKey: ["admin", "vertical-halts"] });
    },
  });

  const busyVertical =
    (haltMutation.isPending &&
      (haltMutation.variables as { vertical: string } | undefined)?.vertical) ||
    (resumeMutation.isPending &&
      (resumeMutation.variables as { vertical: string } | undefined)?.vertical) ||
    null;

  const submitPending = () => {
    if (!pending) return;
    const reason = pending.reason.trim() || null;
    const vars = { vertical: pending.vertical, reason };
    if (pending.action === "halt") haltMutation.mutate(vars);
    else resumeMutation.mutate(vars);
  };

  const halts = haltsQuery.data?.halts ?? {};
  const rows = overview.data?.verticals ?? [];

  return (
    <main data-testid="panel-vertical-halts">
      <p className="text-xs text-muted-foreground mb-3 normal-case tracking-normal">
        Halting a vertical stops the risk-gate chain from approving its
        candidates on the next cycle and immediately cancels every
        resting Kalshi order attached to it. The rest of the portfolio
        keeps trading. Every change writes to <code>admin_audit_log</code>.
      </p>

      {(overview.isLoading || haltsQuery.isLoading) && !haltsQuery.data ? (
        <div className="text-xs text-muted-foreground uppercase tracking-widest">
          Loading…
        </div>
      ) : null}

      <table className="w-full text-xs">
        <thead className="text-muted-foreground uppercase tracking-widest">
          <tr>
            <th className="text-left p-2">Vertical</th>
            <th className="text-left p-2">State</th>
            <th className="text-left p-2">Halted by</th>
            <th className="text-left p-2">Halted at</th>
            <th className="text-left p-2 w-32">Action</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((entry) => {
            const meta = halts[entry.vertical];
            const isHalted = !!meta;
            const busy = busyVertical === entry.vertical;
            const isPrompting = pending?.vertical === entry.vertical;
            return (
              <Fragment key={entry.vertical}>
                <tr
                  className="border-t border-border"
                  data-testid={`row-vertical-halt-${entry.vertical}`}
                >
                  <td className="p-2 uppercase font-bold text-primary">
                    {entry.vertical}
                  </td>
                  <td className="p-2">
                    <span
                      className={`px-2 py-0.5 border text-[10px] uppercase tracking-widest ${
                        isHalted
                          ? "border-destructive text-destructive"
                          : "border-emerald-500/60 text-emerald-500"
                      }`}
                      data-testid={`state-vertical-halt-${entry.vertical}`}
                    >
                      {isHalted ? "Halted" : "Live"}
                    </span>
                  </td>
                  <td className="p-2 text-muted-foreground">
                    <div>{meta?.by_email ?? meta?.by_user_id ?? "—"}</div>
                    {meta?.reason ? (
                      <div
                        className="mt-1 text-foreground italic normal-case tracking-normal"
                        data-testid={`reason-vertical-halt-${entry.vertical}`}
                      >
                        “{meta.reason}”
                      </div>
                    ) : null}
                  </td>
                  <td className="p-2 text-muted-foreground">
                    {meta?.at ? _fmtRelative(meta.at) : "—"}
                  </td>
                  <td className="p-2">
                    {isHalted ? (
                      <button
                        type="button"
                        disabled={busy || isPrompting}
                        onClick={() =>
                          setPending({
                            vertical: entry.vertical,
                            action: "resume",
                            reason: "",
                          })
                        }
                        className="px-2 py-0.5 border border-emerald-500/60 text-emerald-500 text-[10px] uppercase tracking-widest hover:bg-emerald-500/10 disabled:opacity-50"
                        data-testid={`button-resume-${entry.vertical}`}
                      >
                        {busy ? "…" : "Resume"}
                      </button>
                    ) : (
                      <button
                        type="button"
                        disabled={busy || isPrompting}
                        onClick={() =>
                          setPending({
                            vertical: entry.vertical,
                            action: "halt",
                            reason: "",
                          })
                        }
                        className="px-2 py-0.5 border border-destructive text-destructive text-[10px] uppercase tracking-widest hover:bg-destructive/10 disabled:opacity-50"
                        data-testid={`button-halt-${entry.vertical}`}
                      >
                        {busy ? "…" : "Halt"}
                      </button>
                    )}
                  </td>
                </tr>
                {isPrompting && pending ? (
                  <tr
                    className="border-t border-border bg-muted/10"
                    data-testid={`prompt-vertical-${pending.action}-${entry.vertical}`}
                  >
                    <td colSpan={5} className="p-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <label
                          className="text-[10px] uppercase tracking-widest text-muted-foreground"
                          htmlFor={`reason-${entry.vertical}`}
                        >
                          {pending.action === "halt"
                            ? "Halt reason"
                            : "Resume note"}{" "}
                          (optional)
                        </label>
                        <input
                          id={`reason-${entry.vertical}`}
                          type="text"
                          autoFocus
                          maxLength={500}
                          value={pending.reason}
                          onChange={(e) =>
                            setPending({
                              ...pending,
                              reason: e.target.value,
                            })
                          }
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              submitPending();
                            } else if (e.key === "Escape") {
                              e.preventDefault();
                              setPending(null);
                            }
                          }}
                          placeholder={
                            pending.action === "halt"
                              ? "e.g. EIA print pending, sit out NGAS"
                              : "e.g. EIA print done, resuming NGAS"
                          }
                          className="flex-1 min-w-[240px] bg-background border border-border px-2 py-1 text-xs font-mono"
                          data-testid={`input-reason-${entry.vertical}`}
                        />
                        <button
                          type="button"
                          disabled={busy}
                          onClick={submitPending}
                          className={`px-2 py-1 border text-[10px] uppercase tracking-widest disabled:opacity-50 ${
                            pending.action === "halt"
                              ? "border-destructive text-destructive hover:bg-destructive/10"
                              : "border-emerald-500/60 text-emerald-500 hover:bg-emerald-500/10"
                          }`}
                          data-testid={`button-confirm-${pending.action}-${entry.vertical}`}
                        >
                          {busy
                            ? "…"
                            : pending.action === "halt"
                              ? "Confirm halt"
                              : "Confirm resume"}
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => setPending(null)}
                          className="px-2 py-1 border border-border text-muted-foreground text-[10px] uppercase tracking-widest hover:text-foreground disabled:opacity-50"
                          data-testid={`button-cancel-${pending.action}-${entry.vertical}`}
                        >
                          Cancel
                        </button>
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>

      {lastCancel ? (
        <div
          className="mt-3 text-[11px] text-muted-foreground"
          data-testid="text-vertical-halt-cancel-summary"
        >
          Halted <span className="text-primary">{lastCancel.vertical}</span> —
          cancelled {lastCancel.summary.cancelled}/{lastCancel.summary.matched}{" "}
          resting orders
          {lastCancel.summary.failed > 0
            ? ` (${lastCancel.summary.failed} cancel failures)`
            : ""}
          .
        </div>
      ) : null}

      {haltMutation.isError || resumeMutation.isError ? (
        <div className="mt-3 text-xs text-destructive">
          {(
            (haltMutation.error as Error) ??
            (resumeMutation.error as Error)
          )?.message ?? "Toggle failed."}
        </div>
      ) : null}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Early-access leads panel (task #125) — list + filter + CSV export
// ---------------------------------------------------------------------------

type LandingLead = {
  id: number;
  email: string;
  interest: string | null;
  source: string | null;
  created_at: string | null;
  confirmation_sent_at: string | null;
};

type LandingLeadsPage = {
  items: LandingLead[];
  total: number;
  limit: number;
  offset: number;
};

const LEADS_PAGE_SIZES = [25, 50, 100, 200] as const;
const LEADS_INTEREST_NONE_SENTINEL = "__none__";

export function LeadsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const [pageSize, setPageSize] = useState<number>(50);
  const [page, setPage] = useState<number>(0);
  const [interest, setInterest] = useState<string>("");
  const [searchDraft, setSearchDraft] = useState("");
  const [searchApplied, setSearchApplied] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");

  const offset = page * pageSize;

  // The until-date is inclusive in the UI ("on or before this day") but
  // the API treats it as exclusive (`< until`), so push it forward by one
  // day to capture the full UTC day the operator actually selected.
  const untilExclusive = useMemo(() => {
    if (!until) return "";
    const d = new Date(`${until}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return until;
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }, [until]);

  const buildQuery = (overrides: Record<string, string | number> = {}) => {
    const qs = new URLSearchParams();
    qs.set("limit", String(pageSize));
    qs.set("offset", String(offset));
    if (interest) qs.set("interest", interest);
    if (searchApplied) qs.set("search", searchApplied);
    if (since) qs.set("since", since);
    if (untilExclusive) qs.set("until", untilExclusive);
    for (const [k, v] of Object.entries(overrides)) qs.set(k, String(v));
    return qs.toString();
  };

  const listQuery = useQuery<LandingLeadsPage>({
    queryKey: [
      "admin",
      "landing-leads",
      pageSize,
      offset,
      interest,
      searchApplied,
      since,
      untilExclusive,
    ],
    queryFn: () =>
      apiFetch<LandingLeadsPage>(
        `/api/admin/landing-leads?${buildQuery()}`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });

  type InterestList = { items: string[] };
  const interestsQuery = useQuery<InterestList>({
    queryKey: ["admin", "landing-leads", "interests"],
    queryFn: () =>
      apiFetch<InterestList>("/api/admin/landing-leads/interests", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    staleTime: 60_000,
  });

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const onDownloadCsv = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const qs = new URLSearchParams();
      if (interest) qs.set("interest", interest);
      if (searchApplied) qs.set("search", searchApplied);
      if (since) qs.set("since", since);
      if (untilExclusive) qs.set("until", untilExclusive);
      const url = `/api/admin/landing-leads.csv${qs.size > 0 ? `?${qs.toString()}` : ""}`;
      const headers = new Headers({
        accept: "text/csv",
        [REQUEST_ID_HEADER]: newRequestId(),
      });
      const token = await tokenFn();
      if (token) headers.set("authorization", `Bearer ${token}`);
      const res = await fetch(url, { headers });
      if (!res.ok) {
        const id = res.headers.get(REQUEST_ID_HEADER);
        const base = `HTTP ${res.status} ${res.statusText}`;
        throw new Error(id ? `${base} (Reference: ${id})` : base);
      }
      const blob = await res.blob();
      const dispo = res.headers.get("content-disposition") ?? "";
      const match = /filename="?([^";]+)"?/i.exec(dispo);
      const filename = match?.[1] ?? "axiom-landing-leads.csv";
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
    } catch (e) {
      setDownloadError((e as Error)?.message ?? "Download failed.");
    } finally {
      setDownloading(false);
    }
  };

  const items = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;
  const isLoading = listQuery.isLoading;
  const isError = listQuery.isError;
  const knownInterests = interestsQuery.data?.items ?? [];

  const totalPages = total > 0 ? Math.max(1, Math.ceil(total / pageSize)) : 1;
  const goPrev = () => setPage((p) => Math.max(0, p - 1));
  const goNext = () => {
    if (offset + items.length < total) setPage((p) => p + 1);
  };
  const onChangePageSize = (next: number) => {
    setPageSize(next);
    setPage(0);
  };
  const applySearch = () => {
    setSearchApplied(searchDraft.trim());
    setPage(0);
  };
  const clearFilters = () => {
    setInterest("");
    setSearchDraft("");
    setSearchApplied("");
    setSince("");
    setUntil("");
    setPage(0);
  };

  const rangeStart = items.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;
  const hasFilters = !!(interest || searchApplied || since || until);

  return (
    <div className="space-y-3" data-testid="panel-landing-leads">
      <div className="flex flex-wrap items-end gap-2 text-[11px] uppercase tracking-widest text-muted-foreground border border-border bg-card p-2">
        <label className="flex flex-col gap-1">
          <span>Interest area</span>
          <select
            value={interest}
            onChange={(e) => {
              setInterest(e.target.value);
              setPage(0);
            }}
            data-testid="select-leads-interest"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal text-foreground min-w-[12rem]"
          >
            <option value="">all interests</option>
            <option value={LEADS_INTEREST_NONE_SENTINEL}>
              (none selected)
            </option>
            {knownInterests.map((iv) => (
              <option key={iv} value={iv}>
                {iv}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span>From</span>
          <input
            type="date"
            value={since}
            onChange={(e) => {
              setSince(e.target.value);
              setPage(0);
            }}
            data-testid="input-leads-since"
            className="bg-input border border-border px-2 py-1 text-xs tracking-normal text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>To</span>
          <input
            type="date"
            value={until}
            onChange={(e) => {
              setUntil(e.target.value);
              setPage(0);
            }}
            data-testid="input-leads-until"
            className="bg-input border border-border px-2 py-1 text-xs tracking-normal text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span>Email contains</span>
          <input
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") applySearch();
            }}
            onBlur={() => {
              if (searchDraft.trim() !== searchApplied) applySearch();
            }}
            placeholder="substring"
            data-testid="input-leads-search"
            className="bg-input border border-border px-2 py-1 text-xs lowercase tracking-normal w-56 max-w-full normal-case text-foreground"
          />
        </label>
        <button
          type="button"
          onClick={clearFilters}
          disabled={!hasFilters && !searchDraft}
          data-testid="button-leads-clear-filters"
          className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
        >
          Clear
        </button>
        <button
          type="button"
          onClick={onDownloadCsv}
          disabled={downloading || isLoading}
          data-testid="button-leads-download-csv"
          className="border border-primary px-2 py-1 text-[11px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {downloading ? "Exporting…" : "Export CSV"}
        </button>
        {hasFilters ? (
          <span
            className="text-primary text-[10px] tracking-widest"
            data-testid="text-leads-filter-active"
          >
            filters on
          </span>
        ) : null}
      </div>
      {downloadError ? (
        <div
          className="text-destructive text-xs"
          data-testid="text-leads-download-error"
        >
          {downloadError}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] uppercase tracking-widest text-muted-foreground">
        <div data-testid="text-leads-range">
          {isLoading && !listQuery.data
            ? "Loading…"
            : isError
              ? "—"
              : total === 0
                ? "0 leads"
                : `Showing ${rangeStart}–${rangeEnd} of ${total}`}
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1">
            <span>Page size</span>
            <select
              value={pageSize}
              onChange={(e) => onChangePageSize(Number(e.target.value))}
              className="border border-border bg-card px-1 py-0.5 text-[11px] uppercase tracking-widest text-foreground"
              data-testid="select-leads-page-size"
            >
              {LEADS_PAGE_SIZES.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={goPrev}
            disabled={page === 0 || isLoading}
            className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="button-leads-prev"
          >
            ← Prev
          </button>
          <button
            type="button"
            onClick={goNext}
            disabled={offset + items.length >= total || isLoading}
            className="border border-border px-2 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="button-leads-next"
          >
            Next →
          </button>
          <span data-testid="text-leads-page">
            page {page + 1} / {totalPages}
          </span>
        </div>
      </div>
      <div className="border border-border bg-card overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
            <tr>
              <th className="text-left p-2">When</th>
              <th className="text-left p-2">Email</th>
              <th className="text-left p-2">Interest</th>
              <th className="text-left p-2">Source</th>
              <th className="text-left p-2">Confirmed</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && !listQuery.data ? (
              <tr>
                <td
                  colSpan={5}
                  className="p-6 text-center text-muted-foreground"
                >
                  Loading…
                </td>
              </tr>
            ) : isError ? (
              <tr>
                <td colSpan={5} className="p-6 text-center text-destructive">
                  Failed to load leads.
                </td>
              </tr>
            ) : items.length === 0 ? (
              <tr>
                <td
                  colSpan={5}
                  className="p-6 text-center text-muted-foreground"
                >
                  {hasFilters
                    ? "No leads match the current filters."
                    : "No leads yet."}
                </td>
              </tr>
            ) : (
              items.map((lead) => (
                <tr
                  key={lead.id}
                  className="border-t border-border"
                  data-testid={`row-lead-${lead.id}`}
                >
                  <td className="p-2 align-top text-muted-foreground">
                    {lead.created_at?.replace("T", " ").replace("Z", "") ?? "—"}
                  </td>
                  <td
                    className="p-2 align-top text-foreground"
                    data-testid={`text-lead-email-${lead.id}`}
                  >
                    {lead.email}
                  </td>
                  <td className="p-2 align-top text-foreground">
                    {lead.interest ?? (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="p-2 align-top text-muted-foreground">
                    {lead.source ?? "—"}
                  </td>
                  <td className="p-2 align-top text-[10px] text-muted-foreground">
                    {lead.confirmation_sent_at
                      ? lead.confirmation_sent_at
                          .replace("T", " ")
                          .replace("Z", "")
                      : "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Crypto-binary backtest panel — exposes `/api/admin/crypto/backtest` to the
// operator dashboard so they can spot model drift.  Renders the Brier /
// log-loss / accuracy summary plus the reliability bin table for each
// (symbol, timeframe) combination they request.
// ---------------------------------------------------------------------------

const CRYPTO_BACKTEST_SYMBOLS = ["BTC", "ETH"] as const;
const CRYPTO_BACKTEST_TIMEFRAMES = ["5m", "15m", "1h"] as const;

type CryptoBacktestBin = {
  bin_lower: number;
  bin_upper: number;
  mean_predicted: number;
  mean_actual: number;
  count: number;
};

type CryptoBacktestResult = {
  symbol: string;
  timeframe: string;
  n_evaluated: number;
  accuracy: number;
  brier_score: number;
  log_loss: number;
  reliability: CryptoBacktestBin[];
  started_at: string;
  completed_at: string;
};

type CryptoBacktestHistoryItem = {
  id: number;
  symbol: string;
  timeframe: string;
  days: number;
  lookback: number;
  n_evaluated: number;
  accuracy: number;
  brier_score: number;
  log_loss: number;
  started_at: string | null;
  completed_at: string | null;
};

type CryptoBacktestHistoryResponse = {
  symbol: string;
  timeframe: string;
  items: CryptoBacktestHistoryItem[];
};

function fmtMetric(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function CryptoBacktestPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const [symbol, setSymbol] = useState<(typeof CRYPTO_BACKTEST_SYMBOLS)[number]>(
    "BTC",
  );
  const [timeframe, setTimeframe] = useState<
    (typeof CRYPTO_BACKTEST_TIMEFRAMES)[number]
  >("1h");
  const [days, setDays] = useState<number>(14);

  // Track the params the operator has actually committed to running so the
  // backtest only fires when they click "Run", not on every keystroke.
  const [pendingParams, setPendingParams] = useState<{
    symbol: string;
    timeframe: string;
    days: number;
    nonce: number;
  } | null>(null);

  const queryKey = useMemo(
    () =>
      [
        "admin",
        "crypto-backtest",
        pendingParams?.symbol ?? "",
        pendingParams?.timeframe ?? "",
        pendingParams?.days ?? 0,
        pendingParams?.nonce ?? 0,
      ] as const,
    [pendingParams],
  );

  const { data, isFetching, error, refetch } = useQuery<CryptoBacktestResult>({
    queryKey,
    queryFn: () => {
      const params = pendingParams!;
      const qs = new URLSearchParams({
        symbol: params.symbol,
        timeframe: params.timeframe,
        days: String(params.days),
      });
      return apiFetch<CryptoBacktestResult>(
        `/api/admin/crypto/backtest?${qs.toString()}`,
        { getToken: tokenFn },
      );
    },
    enabled: !!isSignedIn && pendingParams !== null,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  // Persisted backtest history for the *currently-selected* pair
  // (independent of `pendingParams` — operators can flip the selector
  // and immediately see the trend without having to press Run first).
  // Refetches automatically when a new run completes via the
  // `data?.completed_at` dependency in the queryKey, so the chart
  // picks up the row this very session just persisted.
  const historyQuery = useQuery<CryptoBacktestHistoryResponse>({
    queryKey: [
      "admin",
      "crypto-backtest-history",
      symbol,
      timeframe,
      data?.completed_at ?? "",
    ] as const,
    queryFn: () => {
      const qs = new URLSearchParams({
        symbol,
        timeframe,
        limit: "50",
      });
      return apiFetch<CryptoBacktestHistoryResponse>(
        `/api/admin/crypto/backtest/history?${qs.toString()}`,
        { getToken: tokenFn },
      );
    },
    enabled: !!isSignedIn,
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  });

  const handleRun = () => {
    setPendingParams({ symbol, timeframe, days, nonce: Date.now() });
  };

  const reliabilityRows = data?.reliability ?? [];

  // Shape the history rows for recharts. Use a 1-based index on the
  // x-axis (cheap + readable) and surface completed_at via the tooltip
  // so operators can still see *when* a run happened on hover.
  const historySeries = useMemo(() => {
    const items = historyQuery.data?.items ?? [];
    return items.map((row, i) => ({
      idx: i + 1,
      brier: row.brier_score,
      log_loss: row.log_loss,
      accuracy: row.accuracy,
      completed_at: row.completed_at,
      n_evaluated: row.n_evaluated,
    }));
  }, [historyQuery.data]);

  return (
    <div className="space-y-3" data-testid="panel-crypto-backtest">
      <p className="text-[11px] text-muted-foreground leading-snug">
        Runs an expanding-window backtest of the crypto-binary predictor
        against historical OHLCV. Surfaces accuracy / Brier / log-loss plus
        the per-bin reliability table — early warning that the live stack is
        drifting versus its historical performance.
      </p>

      <div className="flex flex-wrap items-end gap-3 text-[11px]">
        <label className="flex flex-col gap-1">
          <span className="uppercase tracking-widest text-muted-foreground">
            Symbol
          </span>
          <select
            value={symbol}
            onChange={(e) =>
              setSymbol(
                e.target.value as (typeof CRYPTO_BACKTEST_SYMBOLS)[number],
              )
            }
            className="border border-border bg-input px-2 py-1 text-xs"
            data-testid="select-crypto-backtest-symbol"
          >
            {CRYPTO_BACKTEST_SYMBOLS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="uppercase tracking-widest text-muted-foreground">
            Timeframe
          </span>
          <select
            value={timeframe}
            onChange={(e) =>
              setTimeframe(
                e.target.value as (typeof CRYPTO_BACKTEST_TIMEFRAMES)[number],
              )
            }
            className="border border-border bg-input px-2 py-1 text-xs"
            data-testid="select-crypto-backtest-timeframe"
          >
            {CRYPTO_BACKTEST_TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="uppercase tracking-widest text-muted-foreground">
            Days
          </span>
          <input
            type="number"
            min={1}
            max={90}
            value={days}
            onChange={(e) => {
              const next = Number.parseInt(e.target.value, 10);
              if (Number.isFinite(next)) {
                setDays(Math.max(1, Math.min(90, next)));
              }
            }}
            className="w-20 border border-border bg-input px-2 py-1 text-xs"
            data-testid="input-crypto-backtest-days"
          />
        </label>
        <button
          type="button"
          onClick={handleRun}
          disabled={isFetching}
          className="border border-primary px-3 py-1 text-xs uppercase tracking-widest text-primary hover:bg-primary/10 disabled:opacity-50"
          data-testid="button-crypto-backtest-run"
        >
          {isFetching ? "Running…" : pendingParams ? "Re-run" : "Run"}
        </button>
        {pendingParams && !isFetching && (
          <button
            type="button"
            onClick={() => refetch()}
            className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
            data-testid="button-crypto-backtest-refresh"
          >
            Refresh
          </button>
        )}
      </div>

      {error && (
        <div
          className="border border-loss/60 bg-loss/10 p-2 text-xs text-loss"
          data-testid="crypto-backtest-error"
        >
          Failed to run backtest:{" "}
          {error instanceof Error ? error.message : String(error)}
        </div>
      )}

      {!pendingParams && !error && (
        <div className="text-[11px] text-muted-foreground">
          Choose a symbol / timeframe / window above and press Run.
        </div>
      )}

      <div
        className="border border-border bg-card p-3 space-y-2"
        data-testid="crypto-backtest-history"
      >
        <div className="flex items-baseline justify-between">
          <h3 className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Trend · {symbol} · {timeframe}
          </h3>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {historyQuery.data?.items.length ?? 0} runs
          </span>
        </div>
        {historyQuery.isLoading ? (
          <div
            className="h-32 animate-pulse bg-muted/10"
            data-testid="crypto-backtest-history-loading"
          />
        ) : historyQuery.error ? (
          <div
            className="text-[11px] text-loss"
            data-testid="crypto-backtest-history-error"
          >
            Failed to load backtest history:{" "}
            {historyQuery.error instanceof Error
              ? historyQuery.error.message
              : String(historyQuery.error)}
          </div>
        ) : historySeries.length === 0 ? (
          <div
            className="py-6 text-center text-[11px] text-muted-foreground"
            data-testid="crypto-backtest-history-empty"
          >
            No backtest history yet for this pair — run one above to start
            building a trend.
          </div>
        ) : (
          <div
            className="h-32 w-full"
            data-testid="crypto-backtest-history-chart"
          >
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={historySeries}
                margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="hsl(var(--border))"
                  opacity={0.3}
                />
                <XAxis
                  dataKey="idx"
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  tickLine={false}
                  axisLine={false}
                  width={36}
                  domain={[0, "auto"]}
                />
                <Tooltip
                  cursor={{ stroke: "hsl(var(--border))" }}
                  contentStyle={{
                    background: "hsl(var(--popover))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 11,
                  }}
                  labelFormatter={(_v, payload) => {
                    const completed = payload?.[0]?.payload?.completed_at;
                    return completed
                      ? String(completed)
                          .replace("T", " ")
                          .replace(/\..*$/, "")
                      : "";
                  }}
                  formatter={(value: number, name: string) => [
                    typeof value === "number" ? value.toFixed(4) : value,
                    name,
                  ]}
                />
                <Line
                  type="monotone"
                  dataKey="brier"
                  name="Brier"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="log_loss"
                  name="Log loss"
                  stroke="hsl(var(--accent-cyan, var(--primary)))"
                  strokeWidth={2}
                  strokeDasharray="4 2"
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {data && (
        <div className="space-y-3" data-testid="crypto-backtest-result">
          <div className="grid grid-cols-2 gap-3 text-[11px] md:grid-cols-4">
            <div>
              <div className="uppercase tracking-wider text-muted-foreground">
                Pair
              </div>
              <div
                className="font-mono text-sm"
                data-testid="crypto-backtest-pair"
              >
                {data.symbol} · {data.timeframe}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-wider text-muted-foreground">
                Evaluated
              </div>
              <div
                className="font-mono text-sm"
                data-testid="crypto-backtest-n"
              >
                {data.n_evaluated.toLocaleString()}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-wider text-muted-foreground">
                Accuracy
              </div>
              <div
                className="font-mono text-sm"
                data-testid="crypto-backtest-accuracy"
              >
                {fmtMetric(data.accuracy)}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-wider text-muted-foreground">
                Brier
              </div>
              <div
                className="font-mono text-sm"
                data-testid="crypto-backtest-brier"
              >
                {fmtMetric(data.brier_score)}
              </div>
              <div className="text-[10px] text-muted-foreground">
                log-loss {fmtMetric(data.log_loss)}
              </div>
            </div>
          </div>

          {data.completed_at && (
            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
              Completed{" "}
              {data.completed_at.replace("T", " ").replace(/\..*$/, "")}
            </div>
          )}

          <div className="border border-border bg-card overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-muted/20 text-muted-foreground uppercase tracking-widest">
                <tr>
                  <th className="text-left p-2">Bin</th>
                  <th className="text-right p-2">Mean predicted</th>
                  <th className="text-right p-2">Mean actual</th>
                  <th className="text-right p-2">Count</th>
                </tr>
              </thead>
              <tbody data-testid="crypto-backtest-reliability">
                {reliabilityRows.length === 0 ? (
                  <tr>
                    <td
                      colSpan={4}
                      className="p-3 text-center text-muted-foreground"
                    >
                      {data.n_evaluated === 0
                        ? "No bars matched the requested window — backfill OHLCV or widen the days range."
                        : "No populated reliability bins."}
                    </td>
                  </tr>
                ) : (
                  reliabilityRows.map((row, i) => (
                    <tr
                      key={`${row.bin_lower}-${row.bin_upper}-${i}`}
                      className="border-t border-border"
                      data-testid={`crypto-backtest-bin-${i}`}
                    >
                      <td className="p-2 font-mono">
                        {row.bin_lower.toFixed(2)}–{row.bin_upper.toFixed(2)}
                      </td>
                      <td className="p-2 text-right font-mono">
                        {fmtMetric(row.mean_predicted)}
                      </td>
                      <td className="p-2 text-right font-mono">
                        {fmtMetric(row.mean_actual)}
                      </td>
                      <td className="p-2 text-right font-mono">
                        {row.count.toLocaleString()}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Elo updater scheduler (task #344) — surface the periodic team-rating
// updater's per-cycle counters so operators can see at a glance that team
// ratings are flowing, plus a button to kick a catch-up cycle without
// dropping into curl.
// ---------------------------------------------------------------------------

export type EloUpdaterCycle = {
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number;
  games_examined: number;
  games_updated: number;
  games_skipped: number;
  teams_touched: number;
  errors: string[];
};

export type EloUpdaterStatus = {
  running: boolean;
  cycle_seconds: number;
  last_cycle: EloUpdaterCycle | null;
};

function fmtEloTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toISOString().replace("T", " ").slice(0, 19);
  } catch {
    return String(s);
  }
}

function fmtEloInterval(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = seconds / 60;
  if (m < 60) {
    return Number.isInteger(m) ? `${m}m` : `${m.toFixed(1)}m`;
  }
  const h = m / 60;
  return Number.isInteger(h) ? `${h}h` : `${h.toFixed(1)}h`;
}

export function EloUpdaterDisplay({
  status,
  onRunNow,
  running,
  error,
  result,
}: {
  status: EloUpdaterStatus;
  onRunNow: () => void;
  running: boolean;
  error?: string | null;
  result?: EloUpdaterCycle | null;
}) {
  const last = status.last_cycle ?? null;
  const stateLabel = !status.running
    ? "STOPPED"
    : last && last.errors.length > 0
      ? "ERRORS"
      : last
        ? "OK"
        : "PENDING";
  const stateClass = !status.running
    ? "border-border text-muted-foreground"
    : stateLabel === "ERRORS"
      ? "border-loss/60 bg-loss/10 text-loss"
      : stateLabel === "PENDING"
        ? "border-accent-warning/60 text-accent-warning"
        : "border-accent-cyan/40 text-accent-cyan";

  return (
    <div className="space-y-3" data-testid="panel-elo-updater">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-[11px] text-muted-foreground leading-snug normal-case tracking-normal">
          The periodic Elo updater scans the games table for finished
          matchups that haven&apos;t yet been fed into{" "}
          <code>record_game()</code> and updates per-league team ratings.
          Counters below come from the most recent completed cycle.
        </p>
        <span
          className={`text-[10px] uppercase border px-2 py-0.5 ${stateClass}`}
          data-testid="elo-updater-state"
        >
          {stateLabel}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 text-[11px] md:grid-cols-4">
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Cycle interval
          </div>
          <div
            className="font-mono text-sm text-foreground"
            data-testid="elo-updater-interval"
          >
            {fmtEloInterval(status.cycle_seconds)}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Last run
          </div>
          <div
            className="font-mono text-xs text-foreground"
            data-testid="elo-updater-last-run"
          >
            {last ? fmtEloTime(last.completed_at ?? last.started_at) : "—"}
          </div>
          {last && (
            <div className="text-[10px] text-muted-foreground">
              took {last.duration_seconds.toFixed(2)}s
            </div>
          )}
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Games examined
          </div>
          <div
            className="font-mono text-sm text-foreground"
            data-testid="elo-updater-examined"
          >
            {last ? last.games_examined.toLocaleString() : "—"}
          </div>
          {last && (
            <div className="text-[10px] text-muted-foreground">
              skipped {last.games_skipped.toLocaleString()}
            </div>
          )}
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Games updated
          </div>
          <div
            className={`font-mono text-sm ${
              last && last.games_updated > 0
                ? "text-accent-cyan"
                : "text-foreground"
            }`}
            data-testid="elo-updater-updated"
          >
            {last ? last.games_updated.toLocaleString() : "—"}
          </div>
          {last && (
            <div className="text-[10px] text-muted-foreground">
              {last.teams_touched.toLocaleString()} team-rows touched
            </div>
          )}
        </div>
      </div>

      {!last && (
        <div
          className="border border-border bg-background/50 p-2 text-[11px] text-muted-foreground"
          data-testid="elo-updater-empty"
        >
          The scheduler has not completed a cycle yet — counters will appear
          after the first run finishes (default every 5 minutes), or use
          &quot;Run cycle now&quot; below to kick one immediately.
        </div>
      )}

      {last && last.errors.length > 0 && (
        <div
          className="border border-loss/60 bg-loss/10 p-2 text-[11px] text-loss"
          data-testid="elo-updater-errors"
        >
          <div className="mb-1 uppercase tracking-wider text-[10px]">
            Last cycle errors ({last.errors.length})
          </div>
          <ul className="list-disc list-inside space-y-0.5 normal-case tracking-normal break-words">
            {last.errors.slice(0, 5).map((e, i) => (
              <li key={i} data-testid={`elo-updater-error-${i}`}>
                {e}
              </li>
            ))}
            {last.errors.length > 5 && (
              <li className="text-muted-foreground">
                …and {last.errors.length - 5} more
              </li>
            )}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
        <button
          type="button"
          disabled={running}
          onClick={onRunNow}
          data-testid="button-elo-run-now"
          className="border border-border px-3 py-1.5 text-[10px] uppercase tracking-widest hover:text-primary hover:border-primary disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {running ? "Running…" : "Run cycle now"}
        </button>
        {error && (
          <span
            className="text-loss text-[11px] normal-case tracking-normal"
            data-testid="elo-updater-run-error"
          >
            {error}
          </span>
        )}
        {!error && result && (
          <span
            className="text-[11px] normal-case tracking-normal text-accent-cyan"
            data-testid="elo-updater-run-result"
          >
            Examined {result.games_examined.toLocaleString()} · updated{" "}
            {result.games_updated.toLocaleString()} · skipped{" "}
            {result.games_skipped.toLocaleString()} ·{" "}
            {result.duration_seconds.toFixed(2)}s
            {result.errors.length > 0
              ? ` · ${result.errors.length} error(s)`
              : ""}
          </span>
        )}
      </div>
    </div>
  );
}

export function EloUpdaterPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const queryClient = useQueryClient();
  const queryKey = ["admin", "elo", "status"] as const;

  const { data, isLoading, error } = useQuery<EloUpdaterStatus>({
    queryKey,
    queryFn: () =>
      apiFetch<EloUpdaterStatus>("/api/admin/elo/status", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const runNow = useMutation({
    mutationFn: () =>
      apiFetch<EloUpdaterCycle>("/api/admin/elo/run-cycle", {
        method: "POST",
        getToken: tokenFn,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  if (isLoading) {
    return (
      <div
        className="h-24 animate-pulse bg-muted/30"
        data-testid="elo-updater-loading"
      />
    );
  }
  if (error || !data) {
    return (
      <div className="text-loss text-xs" data-testid="elo-updater-error">
        Failed to load Elo updater status.
      </div>
    );
  }

  return (
    <EloUpdaterDisplay
      status={data}
      onRunNow={() => runNow.mutate()}
      running={runNow.isPending}
      error={
        runNow.isError
          ? ((runNow.error as Error)?.message ?? "Run cycle failed.")
          : null
      }
      result={runNow.isSuccess ? (runNow.data ?? null) : null}
    />
  );
}
