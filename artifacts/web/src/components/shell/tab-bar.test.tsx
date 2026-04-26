import { describe, it, expect, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import { useState } from "react";

import { TabBar, useTabState, type TabDef } from "./tab-bar";

afterEach(() => cleanup());

const TABS: TabDef<"a" | "b" | "c">[] = [
  { key: "a", label: "Alpha" },
  { key: "b", label: "Bravo" },
  { key: "c", label: "Charlie" },
];

function ControlledHarness({ initial }: { initial: "a" | "b" | "c" }) {
  const [active, setActive] = useState<"a" | "b" | "c">(initial);
  return (
    <TabBar
      tabs={TABS}
      active={active}
      onSelect={setActive}
      ariaLabel="Test tabs"
      testIdPrefix="harness"
    />
  );
}

describe("TabBar", () => {
  it("renders one button per tab definition with correct labels and test ids", () => {
    render(<ControlledHarness initial="a" />);
    expect(screen.getByTestId("tabs-harness")).toBeTruthy();
    expect(screen.getByTestId("tab-harness-a").textContent).toBe("Alpha");
    expect(screen.getByTestId("tab-harness-b").textContent).toBe("Bravo");
    expect(screen.getByTestId("tab-harness-c").textContent).toBe("Charlie");
  });

  it("marks the active tab via aria-selected and the inactive tabs as not selected", () => {
    render(<ControlledHarness initial="b" />);
    expect(screen.getByTestId("tab-harness-b").getAttribute("aria-selected"))
      .toBe("true");
    expect(screen.getByTestId("tab-harness-a").getAttribute("aria-selected"))
      .toBe("false");
    // The active tab also picks up the primary palette so it visually
    // stands out — guard against an accidental class drop.
    expect(screen.getByTestId("tab-harness-b").className).toMatch(/primary/);
    expect(screen.getByTestId("tab-harness-a").className).not.toMatch(
      /text-primary\b/,
    );
  });

  it("calls onSelect with the picked tab key when a tab is clicked", () => {
    render(<ControlledHarness initial="a" />);
    fireEvent.click(screen.getByTestId("tab-harness-c"));
    // Re-rendered with new active — Charlie is now selected.
    expect(screen.getByTestId("tab-harness-c").getAttribute("aria-selected"))
      .toBe("true");
  });

  it("uses tablist role + supplied aria-label for accessibility", () => {
    render(<ControlledHarness initial="a" />);
    const list = screen.getByRole("tablist");
    expect(list.getAttribute("aria-label")).toBe("Test tabs");
  });
});

interface UrlHarnessProps {
  initial: "a" | "b" | "c";
}

function UrlHarness({ initial }: UrlHarnessProps) {
  const [tab, setTab] = useTabState<"a" | "b" | "c">(
    ["a", "b", "c"] as const,
    initial,
    "/things",
  );
  return (
    <div>
      <span data-testid="active-tab">{tab}</span>
      <TabBar
        tabs={TABS}
        active={tab}
        onSelect={setTab}
        ariaLabel="URL tabs"
        testIdPrefix="url"
      />
    </div>
  );
}

describe("useTabState", () => {
  it("falls back to the default tab when the URL has no ?tab= parameter", () => {
    const hook = memoryLocation({ path: "/things", record: true });
    render(
      <Router hook={hook.hook}>
        <UrlHarness initial="a" />
      </Router>,
    );
    expect(screen.getByTestId("active-tab").textContent).toBe("a");
  });

  it("hydrates the active tab from a valid ?tab= query parameter", () => {
    const hook = memoryLocation({ path: "/things?tab=b", record: true });
    render(
      <Router hook={hook.hook}>
        <UrlHarness initial="a" />
      </Router>,
    );
    expect(screen.getByTestId("active-tab").textContent).toBe("b");
  });

  it("falls back to the default when ?tab= holds an unknown value", () => {
    const hook = memoryLocation({ path: "/things?tab=zzz", record: true });
    render(
      <Router hook={hook.hook}>
        <UrlHarness initial="a" />
      </Router>,
    );
    // 'zzz' isn't in the tab list — guard against typo'd shareable URLs
    // by falling back to the default rather than blowing up.
    expect(screen.getByTestId("active-tab").textContent).toBe("a");
  });

  it("writes the picked tab into the URL — and clears the param when the default tab is picked again", () => {
    const hook = memoryLocation({ path: "/things", record: true });
    render(
      <Router hook={hook.hook}>
        <UrlHarness initial="a" />
      </Router>,
    );

    fireEvent.click(screen.getByTestId("tab-url-b"));
    expect(screen.getByTestId("active-tab").textContent).toBe("b");
    // The new location should include ?tab=b so the panel is linkable
    // and survives back/forward navigation.
    const afterB = hook.history[hook.history.length - 1] ?? "";
    expect(afterB).toMatch(/^\/things\?tab=b/);

    fireEvent.click(screen.getByTestId("tab-url-a"));
    expect(screen.getByTestId("active-tab").textContent).toBe("a");
    // Switching back to the default drops the query param entirely so
    // the canonical URL stays clean.
    const afterA = hook.history[hook.history.length - 1] ?? "";
    expect(afterA).toBe("/things");
  });
});
