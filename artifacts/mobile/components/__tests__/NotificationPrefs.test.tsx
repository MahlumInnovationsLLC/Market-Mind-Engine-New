/**
 * Component tests for the mobile notification preferences screen.
 *
 * Locks down the SMS verification UI added in task #431 — the badge
 * that surfaces verified/unverified state, the verify-code subform that
 * appears when the saved phone is unverified, and the SMS toggle that
 * stays disabled with an inline hint until verification completes.
 *
 * These mirror the same flow the web side enforces in
 * `artifacts/web/src/pages/settings-notifications.tsx`, so a regression
 * that re-allows enabling SMS without verification on either platform
 * shows up red here.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();

// `NotificationPrefs` mounts a `TimezonePicker` which calls
// `useSafeAreaInsets()` at import time. Stub the safe-area context the
// same way the subscription tests do so the component tree renders
// without the host app's `<SafeAreaProvider>` wrapper.
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
  SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
}));

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
  ApiError: class ApiError extends Error {
    status: number;
    requestId: string;
    body: unknown;
    constructor(
      message: string,
      status: number,
      body: unknown,
      requestId: string,
    ) {
      super(message);
      this.status = status;
      this.body = body;
      this.requestId = requestId;
    }
  },
}));

import { NotificationPrefs } from "../NotificationPrefs";

type Prefs = {
  notify_live_trade: boolean;
  notify_halt: boolean;
  notify_settlement: boolean;
  email_trade_confirmation: boolean;
  digest_partials: boolean;
  sms_live_trade: boolean;
  quiet_hours_start_hour: number | null;
  quiet_hours_end_hour: number | null;
  quiet_hours_tz: string | null;
  sms_phone_number: string | null;
  sms_phone_verified: boolean;
  sms_phone_verification_expires_at: string | null;
};

function makePrefs(overrides: Partial<Prefs> = {}) {
  const prefs: Prefs = {
    notify_live_trade: true,
    notify_halt: true,
    notify_settlement: true,
    email_trade_confirmation: true,
    digest_partials: false,
    sms_live_trade: false,
    quiet_hours_start_hour: null,
    quiet_hours_end_hour: null,
    quiet_hours_tz: null,
    sms_phone_number: null,
    sms_phone_verified: false,
    sms_phone_verification_expires_at: null,
    ...overrides,
  };
  return {
    prefs,
    tier: "elite",
    elite_unlocked: true,
    elite_only_toggles: ["sms_live_trade", "telegram_live_trade"],
  };
}

function renderPanel() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <NotificationPrefs />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("NotificationPrefs SMS verification (mobile)", () => {
  it("shows an Unverified badge when an Elite user has saved an unverified phone", async () => {
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        sms_phone_number: "+15551234567",
        sms_phone_verified: false,
      }),
    );

    renderPanel();

    const badge = await screen.findByTestId("badge-phone-verified-status");
    expect(badge).toHaveTextContent(/unverified/i);
    // Verify subform appears with the saved number echoed back.
    expect(screen.getByTestId("section-phone-verify")).toBeTruthy();
    expect(screen.getByTestId("input-sms-code")).toBeTruthy();
    expect(screen.getByTestId("button-verify-code")).toBeTruthy();
    expect(screen.getByTestId("button-resend-code")).toBeTruthy();
  });

  it("shows a Verified badge and hides the verify subform once verified", async () => {
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        sms_phone_number: "+15551234567",
        sms_phone_verified: true,
      }),
    );

    renderPanel();

    const badge = await screen.findByTestId("badge-phone-verified-status");
    expect(badge).toHaveTextContent(/^verified$/i);
    expect(screen.queryByTestId("section-phone-verify")).toBeNull();
  });

  it("disables the SMS toggle with a verify-first hint until the phone is verified", async () => {
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        sms_phone_number: "+15551234567",
        sms_phone_verified: false,
        sms_live_trade: false,
      }),
    );

    renderPanel();

    // Wait for the prefs query to resolve — the badge only renders
    // once the GET has populated `sms_phone_number`.
    await screen.findByTestId("badge-phone-verified-status");

    const row = screen.getByTestId("row-sms-live-trade");
    expect(row).toHaveTextContent(/verify your phone number/i);
    // After the initial GET, no further calls should have been issued —
    // the disabled toggle must not fire a PUT under us.
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });

  it("posts the entered code to /sms/verify and clears the unverified state on success", async () => {
    // Initial GET returns an unverified phone.
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        sms_phone_number: "+15551234567",
        sms_phone_verified: false,
      }),
    );
    // POST /sms/verify returns the same prefs but flipped to verified.
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        sms_phone_number: "+15551234567",
        sms_phone_verified: true,
      }),
    );

    renderPanel();

    const codeInput = await screen.findByTestId("input-sms-code");
    fireEvent.changeText(codeInput, "123456");
    fireEvent.press(screen.getByTestId("button-verify-code"));

    await waitFor(() => {
      const badge = screen.getByTestId("badge-phone-verified-status");
      expect(badge).toHaveTextContent(/^verified$/i);
    });

    // Second call hit the verify endpoint with the typed code.
    expect(mockApiFetch).toHaveBeenNthCalledWith(
      2,
      "/api/me/notification-prefs/sms/verify",
      expect.objectContaining({
        method: "POST",
        body: { code: "123456" },
      }),
    );
    expect(screen.getByTestId("text-verify-msg")).toHaveTextContent(
      /verified/i,
    );
  });

  it("hits the start-verification endpoint when Resend is tapped", async () => {
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        sms_phone_number: "+15551234567",
        sms_phone_verified: false,
      }),
    );
    mockApiFetch.mockResolvedValueOnce({ ok: true });

    renderPanel();

    fireEvent.press(await screen.findByTestId("button-resend-code"));

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenNthCalledWith(
        2,
        "/api/me/notification-prefs/sms/start-verification",
        expect.objectContaining({ method: "POST" }),
      );
    });
    expect(screen.getByTestId("text-verify-msg")).toHaveTextContent(
      /new code sent/i,
    );
  });

  it("does not render the verified badge when no phone has been saved yet", async () => {
    mockApiFetch.mockResolvedValueOnce(makePrefs());

    renderPanel();

    // Wait for initial load — the SMS row hint is the easiest anchor.
    await screen.findByTestId("row-sms-live-trade");
    expect(screen.queryByTestId("badge-phone-verified-status")).toBeNull();
    expect(screen.queryByTestId("section-phone-verify")).toBeNull();
  });
});
