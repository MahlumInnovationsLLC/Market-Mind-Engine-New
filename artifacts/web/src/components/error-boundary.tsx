import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = {
  /** Rendered tree. Errors thrown by descendants are caught. */
  children: ReactNode;
  /**
   * Optional context label (e.g. "ClerkApp") used in console output and
   * the default error UI so on-call can tell at a glance which boundary
   * tripped.
   */
  label?: string;
  /**
   * Optional render-prop fallback. If omitted, a sensible centered
   * "Something went wrong" card is rendered. Receives the captured
   * error and a `reset()` callback that clears the boundary's error
   * state so the children can try to mount again.
   */
  fallback?: (args: { error: Error; reset: () => void }) => ReactNode;
};

type State = { error: Error | null };

/**
 * Generic error boundary used to wrap the lazy ClerkApp chunk so that
 * a chunk-load failure, a missing-env crash inside the module body
 * (`throw new Error("Missing VITE_CLERK_PUBLISHABLE_KEY in env")`),
 * or a render-time crash inside Clerk surfaces as a visible message
 * instead of a silent black screen.
 *
 * Reload simply calls `window.location.reload()` because chunk
 * failures are most often resolved by a fresh fetch after a deploy
 * rolled the asset hashes.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Forward to the console so on-call still sees the stack in
    // browser devtools / Sentry breadcrumbs even when the user only
    // sees the friendly fallback.
    // eslint-disable-next-line no-console
    console.error(
      `[ErrorBoundary${this.props.label ? `:${this.props.label}` : ""}]`,
      error,
      info.componentStack,
    );
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    if (this.props.fallback) {
      return this.props.fallback({ error, reset: this.reset });
    }

    return (
      <div
        role="alert"
        className="min-h-[100dvh] bg-background text-foreground flex items-center justify-center p-6 font-mono"
      >
        <div className="max-w-md w-full rounded-lg border border-border bg-card text-card-foreground p-6 shadow-lg space-y-4">
          <div className="space-y-1">
            <h1 className="text-lg font-semibold">Something went wrong</h1>
            <p className="text-sm text-muted-foreground">
              We couldn't load this part of the app. This usually clears
              up after a refresh.
            </p>
          </div>
          <pre className="text-xs whitespace-pre-wrap break-words rounded-md bg-muted p-3 text-muted-foreground max-h-40 overflow-auto">
            {error.message || String(error)}
          </pre>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="inline-flex items-center justify-center rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
            >
              Reload page
            </button>
            <button
              type="button"
              onClick={this.reset}
              className="inline-flex items-center justify-center rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-muted"
            >
              Try again
            </button>
          </div>
        </div>
      </div>
    );
  }
}
