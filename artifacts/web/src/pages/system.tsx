import { PlaceholderPage } from "@/components/shell/placeholder-page";

export default function SystemPage() {
  return (
    <PlaceholderPage
      eyebrow="System"
      title="System health"
      description="Health snapshot, feed status, model inventory, and ready gate. Wired up in the System rebuild."
      testId="page-system"
    />
  );
}
