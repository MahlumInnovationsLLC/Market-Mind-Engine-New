import { PageHeader } from "./page-header";
import { Skeleton } from "@/components/ui/skeleton";

interface PlaceholderPageProps {
  eyebrow: string;
  title: string;
  description?: string;
  testId: string;
}

export function PlaceholderPage({
  eyebrow,
  title,
  description,
  testId,
}: PlaceholderPageProps) {
  return (
    <div data-testid={testId} className="font-mono">
      <PageHeader
        eyebrow={eyebrow}
        title={title}
        description={
          description ?? "This view is being assembled by the next migration task."
        }
      />
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
        <Skeleton className="h-40" />
        <Skeleton className="h-72 lg:col-span-2" />
        <Skeleton className="h-72" />
      </div>
    </div>
  );
}
