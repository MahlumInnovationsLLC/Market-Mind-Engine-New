import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

const { authState } = vi.hoisted(() => ({
  authState: { isSignedIn: false as boolean },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => ({ isSignedIn: authState.isSignedIn }),
  // SignUpButton wraps a child element (the actual button); the mock just
  // forwards children so we can find the inner test-id.
  SignUpButton: ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children),
}));

// PublicNav consults a cookie heuristic; pin it so the test doesn't
// depend on document.cookie state.
vi.mock("@/lib/clerk-session", () => ({
  hasClerkSession: () => false,
}));

import AboutPage from "./about";

afterEach(() => cleanup());

function renderAt(path: string) {
  const hook = memoryLocation({ path, record: true });
  return render(
    <Router hook={hook.hook}>
      <AboutPage />
    </Router>,
  );
}

describe("AboutPage", () => {
  it("renders the hero, mission, and three principle cards", () => {
    authState.isSignedIn = false;
    renderAt("/about");

    expect(screen.getByTestId("text-about-title").textContent).toMatch(
      /trader/i,
    );
    expect(screen.getByTestId("card-principle-swarm")).toBeTruthy();
    expect(screen.getByTestId("card-principle-gates")).toBeTruthy();
    expect(screen.getByTestId("card-principle-loop")).toBeTruthy();
    expect(screen.getByText(/Past performance is not predictive/i))
      .toBeTruthy();
  });

  it("shows a 'Create account' CTA when the user is signed out", () => {
    authState.isSignedIn = false;
    renderAt("/about");
    expect(screen.getByTestId("button-cta-signup")).toBeTruthy();
    expect(screen.queryByTestId("link-cta-console")).toBeNull();
  });

  it("shows an 'Open console' link when the user is already signed in", () => {
    authState.isSignedIn = true;
    renderAt("/about");
    const link = screen.getByTestId("link-cta-console");
    expect(link.getAttribute("href")).toBe("/app");
    expect(screen.queryByTestId("button-cta-signup")).toBeNull();
  });
});
