import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { PageHeader } from "./page-header";
import { PlaceholderPage } from "./placeholder-page";

afterEach(() => cleanup());

describe("PageHeader", () => {
  it("renders the title plus optional eyebrow, description, and actions", () => {
    render(
      <PageHeader
        eyebrow="EYEBROW"
        title="Dashboard"
        description="A short subtitle"
        actions={<button data-testid="hdr-action">Click</button>}
      />,
    );
    expect(screen.getByTestId("shell-page-header")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: "Dashboard" }),
    ).toBeTruthy();
    expect(screen.getByText("EYEBROW")).toBeTruthy();
    expect(screen.getByText("A short subtitle")).toBeTruthy();
    expect(screen.getByTestId("hdr-action")).toBeTruthy();
  });

  it("renders only the title when optional props are omitted", () => {
    const { container } = render(<PageHeader title="Bare" />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Bare" }),
    ).toBeTruthy();
    // No actions container, no eyebrow, no description — confirm we
    expect(container.querySelectorAll("p").length).toBe(0);
  });
});

describe("PlaceholderPage", () => {
  it("renders the supplied test id and uses the description override when provided", () => {
    render(
      <PlaceholderPage
        eyebrow="WIP"
        title="Settings"
        description="Custom copy"
        testId="page-settings-placeholder"
      />,
    );
    expect(screen.getByTestId("page-settings-placeholder")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: "Settings" }),
    ).toBeTruthy();
    expect(screen.getByText("Custom copy")).toBeTruthy();
  });

  it("falls back to the standard 'next migration task' copy when no description is given", () => {
    render(
      <PlaceholderPage
        eyebrow="WIP"
        title="Soon"
        testId="page-soon-placeholder"
      />,
    );
    // The default copy must stay stable so the operator knows the
    // empty page is intentional and not a render bug.
    expect(
      screen.getByText(/being assembled by the next migration task/i),
    ).toBeTruthy();
  });
});
