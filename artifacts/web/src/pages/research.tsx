import { useGetMyAccess, getGetMyAccessQueryKey } from "@workspace/api-client-react";
import AutoResearchPanel from "@/components/intelligence/autoresearch-panel";

export default function ResearchPage() {
  const { data } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 60_000 },
  });
  const isAdmin = !!data?.is_admin;
  return (
    <div className="space-y-4 font-mono" data-testid="page-research">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Auto-research</h1>
        <p className="text-xs uppercase tracking-widest text-muted-foreground">
          Open directives · review &amp; promote pending findings
        </p>
      </header>
      <AutoResearchPanel isAdmin={isAdmin} />
    </div>
  );
}
