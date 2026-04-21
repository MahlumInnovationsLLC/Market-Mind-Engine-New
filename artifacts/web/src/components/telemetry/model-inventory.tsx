import { useGetModelsInventory } from "@workspace/api-client-react";

function humanBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export default function ModelInventory() {
  const { data, isLoading } = useGetModelsInventory();

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-56 bg-card" />;
  }

  if (!data) return null;

  const exports = data.exports;
  const cutover = data.cutover ?? [];
  const totalArchive =
    (exports?.models_archive_total_bytes ?? 0) + (exports?.source_archive_total_bytes ?? 0);

  return (
    <div className="border border-border bg-card">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          COLD_STORAGE
        </h2>
        <span className="text-xs text-muted-foreground">{humanBytes(totalArchive)}</span>
      </div>
      <div className="p-3 text-xs space-y-3">
        <div>
          <div className="flex justify-between mb-1">
            <span className="text-muted-foreground uppercase tracking-wider text-[10px]">
              MODELS_ARCHIVE
            </span>
            <span className="text-primary">
              {humanBytes(exports?.models_archive_total_bytes)}
            </span>
          </div>
          <div className="text-[10px] text-muted-foreground/70">
            {exports?.files?.filter((f) => f.name.startsWith("models_part_")).length ?? 0} parts
          </div>
        </div>
        <div>
          <div className="flex justify-between mb-1">
            <span className="text-muted-foreground uppercase tracking-wider text-[10px]">
              SOURCE_ARCHIVE
            </span>
            <span className="text-primary">
              {humanBytes(exports?.source_archive_total_bytes)}
            </span>
          </div>
          <div className="text-[10px] text-muted-foreground/70">
            {exports?.files?.filter((f) => f.name.startsWith("source_part_")).length ?? 0} parts
          </div>
        </div>

        {cutover.length > 0 ? (
          <div className="pt-2 border-t border-border/50">
            <div className="text-muted-foreground uppercase tracking-wider text-[10px] mb-1">
              CUTOVER
            </div>
            <ul className="space-y-0.5">
              {cutover.map((c) => (
                <li key={c.name} className="flex justify-between gap-2">
                  <span className="truncate text-foreground/80" title={c.name}>
                    {c.name}
                  </span>
                  <span className="text-muted-foreground shrink-0">{humanBytes(c.size)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {data.buckets?.length ? (
          <div className="pt-2 border-t border-border/50 text-[10px] text-muted-foreground">
            BUCKET_ROOT: {data.buckets.map((b) => b.name).join(" · ")}
          </div>
        ) : null}
      </div>
    </div>
  );
}
