import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";

type FreshnessStatus = "fresh" | "stale" | "missing";

interface SourceFreshness {
  observed_at: string | null;
  status: FreshnessStatus;
  max_age_s: number;
  // HRRR-only
  run_cycle?: string | null;
  run_time?: string | null;
}

interface CityFreshness {
  code: string;
  name: string;
  sources: Record<string, SourceFreshness>;
}

interface FreshnessResponse {
  as_of: string;
  freshness_windows_s: Record<string, number>;
  cities: CityFreshness[];
}

const SOURCE_ORDER: Array<{ key: string; label: string }> = [
  { key: "open-meteo", label: "Open-Meteo" },
  { key: "nws", label: "NWS" },
  { key: "tomorrow.io", label: "Tomorrow.io" },
  { key: "accuweather", label: "AccuWeather" },
  { key: "hrrr", label: "HRRR" },
];

function formatAge(isoTs: string | null, now: number): string {
  if (!isoTs) return "—";
  const t = new Date(isoTs).getTime();
  if (!Number.isFinite(t)) return "—";
  const sec = Math.max(0, Math.round((now - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hrs = Math.round(min / 6) / 10;
  return `${hrs}h ago`;
}

function formatTimestamp(isoTs: string | null): string {
  if (!isoTs) return "no data";
  const d = new Date(isoTs);
  if (Number.isNaN(d.getTime())) return isoTs;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

function statusClasses(status: FreshnessStatus): string {
  switch (status) {
    case "fresh":
      return "border-success/40 bg-success/10 text-success";
    case "stale":
      return "border-warning/40 bg-warning/10 text-warning";
    case "missing":
    default:
      return "border-destructive/40 bg-destructive/10 text-destructive";
  }
}

function statusLabel(status: FreshnessStatus): string {
  switch (status) {
    case "fresh":
      return "FRESH";
    case "stale":
      return "STALE";
    case "missing":
    default:
      return "MISSING";
  }
}

interface BadgeProps {
  source: { key: string; label: string };
  data: SourceFreshness | undefined;
  now: number;
}

function FreshnessBadge({ source, data, now }: BadgeProps) {
  const status: FreshnessStatus = data?.status ?? "missing";
  const observedAt = data?.observed_at ?? null;
  const isHrrr = source.key === "hrrr";
  const runCycle = isHrrr ? data?.run_cycle ?? null : null;

  return (
    <div
      className={`rounded-md border px-3 py-2 text-xs font-mono ${statusClasses(status)}`}
      data-testid={`freshness-badge-${source.key}`}
      data-status={status}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold tracking-wider text-foreground">
          {source.label}
        </span>
        <span className="uppercase tracking-widest text-[10px]">
          {statusLabel(status)}
        </span>
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-foreground/80">
        <span title={observedAt ?? undefined}>
          {formatTimestamp(observedAt)}
        </span>
        <span className="text-muted-foreground">{formatAge(observedAt, now)}</span>
      </div>
      {isHrrr && (
        <div className="mt-1 flex items-center justify-between gap-2">
          <span className="text-muted-foreground">run cycle</span>
          <span
            className="font-semibold text-foreground"
            data-testid="freshness-hrrr-cycle"
          >
            {runCycle ?? "—"}
          </span>
        </div>
      )}
    </div>
  );
}

export default function WeatherPage() {
  const query = useQuery<FreshnessResponse>({
    queryKey: ["weather-freshness"],
    queryFn: () =>
      customFetch<FreshnessResponse>(
        `${import.meta.env.BASE_URL}api/weather/freshness`,
      ),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const now = useMemo(() => {
    if (!query.data?.as_of) return Date.now();
    const t = new Date(query.data.as_of).getTime();
    return Number.isFinite(t) ? t : Date.now();
  }, [query.data?.as_of]);

  return (
    <div className="space-y-6 font-mono" data-testid="weather-page">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Weather</h1>
        <p className="text-sm text-muted-foreground">
          Per-source freshness for every Kalshi weather city. Green = fresh
          inside the poller's expected window, yellow = stale, red = no data.
          The HRRR badge shows the NOMADS run cycle so you can confirm we're
          trading off the latest published cycle.
        </p>
      </header>

      {query.isLoading && (
        <div
          className="text-xs uppercase tracking-widest text-muted-foreground"
          aria-live="polite"
        >
          Loading freshness…
        </div>
      )}

      {query.isError && (
        <div
          className="rounded-md border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive"
          data-testid="weather-error"
        >
          Failed to load weather freshness:{" "}
          {(query.error as Error)?.message ?? "unknown error"}
        </div>
      )}

      {query.data && (
        <>
          <div className="text-xs text-muted-foreground">
            As of {formatTimestamp(query.data.as_of)}
            {" · "}refreshing every 30s
          </div>

          <div className="space-y-4">
            {query.data.cities.map((city) => (
              <section
                key={city.code}
                className="rounded-lg border border-border bg-card p-4"
                data-testid={`city-${city.code}`}
              >
                <div className="mb-3 flex items-baseline justify-between gap-3">
                  <h2 className="text-lg font-semibold">
                    {city.name}{" "}
                    <span className="text-xs text-muted-foreground">
                      {city.code}
                    </span>
                  </h2>
                </div>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
                  {SOURCE_ORDER.map((src) => (
                    <FreshnessBadge
                      key={src.key}
                      source={src}
                      data={city.sources[src.key]}
                      now={now}
                    />
                  ))}
                </div>
              </section>
            ))}
            {query.data.cities.length === 0 && (
              <div className="text-sm text-muted-foreground">
                No weather cities configured.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
