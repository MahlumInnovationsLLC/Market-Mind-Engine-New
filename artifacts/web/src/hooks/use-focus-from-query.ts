import { useEffect } from "react";
import { useLocation, useSearch } from "wouter";

/**
 * Drives the deep-link focus highlight: when the current URL has a
 * `?focus=<id>` search param, scroll the matching `[data-focus-id="<id>"]`
 * element into view, briefly flash it, and strip the param so re-renders
 * don't re-trigger.
 *
 * Originally lived inline in the dashboard (task #142). Extracted so other
 * pages that render the same row types (trading, system telemetry tabs)
 * can adopt the same behaviour with a one-liner.
 *
 * The hook polls for the target element for up to ~3s so freshly mounted
 * lists / tab panels have time to render before the search param is
 * cleared.
 */
export function useFocusFromQuery(): void {
  const search = useSearch();
  const [location, setLocation] = useLocation();

  useEffect(() => {
    const focus = new URLSearchParams(search).get("focus");
    if (!focus) return;

    let cancelled = false;
    let attempts = 0;

    const tryFocus = () => {
      if (cancelled) return;
      const el = document.querySelector<HTMLElement>(
        `[data-focus-id="${CSS.escape(focus)}"]`,
      );
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.dataset.focusFlash = "true";
        // Note: deliberately not stored on the cleanup-cancelled side.
        // Stripping the focus param below re-runs this effect and would
        // otherwise cancel the flash timer immediately, leaving the
        // highlight class stuck on the row.
        window.setTimeout(() => {
          delete el.dataset.focusFlash;
        }, 2500);
        // Strip the focus param so re-renders don't re-trigger.
        const next = new URLSearchParams(search);
        next.delete("focus");
        const qs = next.toString();
        setLocation(qs ? `${location}?${qs}` : location, { replace: true });
        return;
      }
      attempts += 1;
      if (attempts < 30) {
        window.setTimeout(tryFocus, 100);
      }
    };
    // Defer to next paint so freshly-mounted lists are in the DOM.
    const start = window.setTimeout(tryFocus, 50);
    return () => {
      cancelled = true;
      window.clearTimeout(start);
    };
  }, [search, location, setLocation]);
}
