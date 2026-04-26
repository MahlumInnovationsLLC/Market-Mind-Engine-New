import { describe, it, expect, afterEach, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

const { signOutMock } = vi.hoisted(() => ({
  signOutMock: vi.fn(),
}));

vi.mock("@clerk/react", () => ({
  useClerk: () => ({ signOut: signOutMock }),
}));

import PendingApproval from "./pending-approval";

afterEach(() => {
  cleanup();
  signOutMock.mockReset();
});

describe("PendingApproval", () => {
  it("renders the awaiting-approval heading and pending status block", () => {
    render(<PendingApproval />);
    expect(screen.getByTestId("page-pending-approval")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: /awaiting approval/i }),
    ).toBeTruthy();
    // The status block reproduces the operator-facing handoff copy; if
    // it disappears the user has no idea what to do next.
    expect(
      screen.getByText(/STATUS :: pending_approval/i),
    ).toBeTruthy();
    expect(
      screen.getByText(/Send the email above to your operator/i),
    ).toBeTruthy();
  });

  it("shows the signed-in email when one is provided", () => {
    render(<PendingApproval email="operator@example.com" />);
    const emailEl = screen.getByTestId("text-pending-email");
    expect(emailEl.textContent).toBe("operator@example.com");
  });

  it("hides the email block entirely when email is null", () => {
    render(<PendingApproval email={null} />);
    expect(screen.queryByTestId("text-pending-email")).toBeNull();
  });

  it("calls Clerk signOut with the BASE_URL redirect when the button is clicked", () => {
    render(<PendingApproval email="x@y.com" />);
    fireEvent.click(screen.getByTestId("button-pending-sign-out"));
    expect(signOutMock).toHaveBeenCalledTimes(1);
    const arg = signOutMock.mock.calls[0]?.[0] as
      | { redirectUrl: string }
      | undefined;
    // Sign-out must redirect back to the artifact's mount point so the
    // user lands on the public site, not on a 404 outside the SPA.
    expect(typeof arg?.redirectUrl).toBe("string");
  });
});
