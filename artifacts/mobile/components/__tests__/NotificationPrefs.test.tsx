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
 *
 * Also covers the device-timezone-mismatch banner added in task #527 —
 * the one-tap "switch quiet hours to <device tz>" suggestion that fires
 * when a traveler's device clock no longer matches `quiet_hours_tz`.
 */
import AsyncStorage from "@react-native-async-storage/async-storage";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react-native";
import React from "react";
import { DeviceEventEmitter } from "react-native";

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

describe("NotificationPrefs device-tz banner (mobile)", () => {
  // Pin the device's IANA zone for the duration of each test so we can
  // simulate a traveler whose clock has flipped without depending on the
  // host machine's actual timezone.
  //
  // The component reads it from `Intl.DateTimeFormat().resolvedOptions()
  // .timeZone` (no-arg constructor). We proxy `Intl.DateTimeFormat` so
  // the no-arg form returns the spoofed zone, while any call with args
  // (e.g. `offsetForZone`'s `{ timeZone: "..." }` formatter) is forwarded
  // to the real implementation untouched.
  const RealDateTimeFormat = Intl.DateTimeFormat;
  function mockDeviceTimezone(zone: string): void {
    const FakeDateTimeFormat = function (
      ...args: ConstructorParameters<typeof Intl.DateTimeFormat>
    ) {
      const fmt = new RealDateTimeFormat(...args);
      if (args.length === 0) {
        const realResolved = fmt.resolvedOptions.bind(fmt);
        fmt.resolvedOptions = () => ({ ...realResolved(), timeZone: zone });
      }
      return fmt;
    } as unknown as typeof Intl.DateTimeFormat;
    // Preserve the static surface so `Intl.DateTimeFormat.supportedLocalesOf`
    // etc. keep working if anything reaches for them.
    Object.setPrototypeOf(FakeDateTimeFormat, RealDateTimeFormat);
    (Intl as { DateTimeFormat: typeof Intl.DateTimeFormat }).DateTimeFormat =
      FakeDateTimeFormat;
  }

  beforeEach(async () => {
    await AsyncStorage.clear();
  });

  afterEach(() => {
    (Intl as { DateTimeFormat: typeof Intl.DateTimeFormat }).DateTimeFormat =
      RealDateTimeFormat;
  });

  it("does not show the banner when the device tz matches quiet_hours_tz", async () => {
    mockDeviceTimezone("America/New_York");
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/New_York",
      }),
    );

    renderPanel();

    await screen.findByTestId("row-sms-live-trade");
    expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();
  });

  it("does not show the banner when quiet hours are disabled (no saved tz)", async () => {
    mockDeviceTimezone("Asia/Tokyo");
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: null,
        quiet_hours_end_hour: null,
        quiet_hours_tz: null,
      }),
    );

    renderPanel();

    await screen.findByTestId("row-sms-live-trade");
    expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();
  });

  it("surfaces the device's zone when it differs from quiet_hours_tz", async () => {
    mockDeviceTimezone("Asia/Tokyo");
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/Los_Angeles",
      }),
    );

    renderPanel();

    const banner = await screen.findByTestId("banner-device-tz-mismatch");
    // Both zones (and the friendly city for the saved one) appear in
    // the body so the user can confirm before tapping Switch.
    expect(banner).toHaveTextContent(/Tokyo/);
    expect(banner).toHaveTextContent(/Asia\/Tokyo/);
    expect(banner).toHaveTextContent(/America\/Los_Angeles/);
    expect(screen.getByTestId("button-tz-banner-switch")).toBeTruthy();
    expect(screen.getByTestId("button-tz-banner-dismiss")).toBeTruthy();
  });

  it("PUTs the device tz when Switch is tapped and clears any prior dismissal", async () => {
    mockDeviceTimezone("Asia/Tokyo");
    // Pre-seed a stale dismissal for an unrelated pair so we can confirm
    // it gets wiped when the user opts in to switching.
    await AsyncStorage.setItem(
      "axiom.notifications.tzBannerDismissedPair.v1",
      JSON.stringify({ device: "Europe/Paris", quiet: "America/New_York" }),
    );
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/Los_Angeles",
      }),
    );
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "Asia/Tokyo",
      }),
    );

    renderPanel();

    const switchBtn = await screen.findByTestId("button-tz-banner-switch");
    fireEvent.press(switchBtn);

    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenNthCalledWith(
        2,
        "/api/me/notification-prefs",
        expect.objectContaining({
          method: "PUT",
          body: expect.objectContaining({
            quiet_hours_tz: "Asia/Tokyo",
            quiet_hours_tz_present: true,
          }),
        }),
      );
    });

    // Saved zone now matches the device, so the banner is gone.
    await waitFor(() => {
      expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();
    });
    // Dismissal store was cleared as part of the opt-in.
    expect(
      await AsyncStorage.getItem(
        "axiom.notifications.tzBannerDismissedPair.v1",
      ),
    ).toBeNull();
  });

  it("hides the banner and persists the dismissed pair when Dismiss is tapped", async () => {
    mockDeviceTimezone("Asia/Tokyo");
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/Los_Angeles",
      }),
    );

    renderPanel();

    const dismissBtn = await screen.findByTestId("button-tz-banner-dismiss");
    fireEvent.press(dismissBtn);

    await waitFor(() => {
      expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();
    });

    const stored = await AsyncStorage.getItem(
      "axiom.notifications.tzBannerDismissedPair.v1",
    );
    expect(stored).not.toBeNull();
    expect(JSON.parse(stored!)).toEqual({
      device: "Asia/Tokyo",
      quiet: "America/Los_Angeles",
    });

    // Dismiss must not fire a PUT — the saved zone stays put.
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });

  it("does not re-show the banner on remount when the same pair was already dismissed", async () => {
    mockDeviceTimezone("Asia/Tokyo");
    await AsyncStorage.setItem(
      "axiom.notifications.tzBannerDismissedPair.v1",
      JSON.stringify({ device: "Asia/Tokyo", quiet: "America/Los_Angeles" }),
    );
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/Los_Angeles",
      }),
    );

    renderPanel();

    // Wait for the rest of the card to render so we know the dismissal
    // load has had time to settle, then assert the banner stays absent.
    await screen.findByTestId("row-sms-live-trade");
    await waitFor(() => {
      expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();
    });
  });

  it("re-renders the banner without remount when the OS broadcasts a timezone change mid-session (task #634)", async () => {
    // Start in LA — matches the saved zone, so the banner stays hidden.
    mockDeviceTimezone("America/Los_Angeles");
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/Los_Angeles",
      }),
    );

    renderPanel();

    await screen.findByTestId("row-sms-live-trade");
    expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();

    // Simulate the traveler's plane landing: the device clock flips to
    // Tokyo and the OS broadcasts its timezone-change notification. The
    // banner must appear immediately, *without* needing AppState to
    // toggle to "active" (no remount, no foregrounding).
    mockDeviceTimezone("Asia/Tokyo");
    act(() => {
      DeviceEventEmitter.emit("timezoneDidChange");
    });

    const banner = await screen.findByTestId("banner-device-tz-mismatch");
    expect(banner).toHaveTextContent(/Tokyo/);
    expect(banner).toHaveTextContent(/America\/Los_Angeles/);
    // The Android-side event name should also drive the same refresh
    // path. Flip back to LA via the Android broadcast and confirm the
    // banner clears, again without remount.
    mockDeviceTimezone("America/Los_Angeles");
    act(() => {
      DeviceEventEmitter.emit("timezoneChanged");
    });
    await waitFor(() => {
      expect(screen.queryByTestId("banner-device-tz-mismatch")).toBeNull();
    });
  });

  it("re-shows the banner when the device tz differs from a previously dismissed pair", async () => {
    mockDeviceTimezone("Asia/Tokyo");
    // User dismissed a Paris→NY pair on a previous trip; that has no
    // bearing on the current Tokyo→LA mismatch.
    await AsyncStorage.setItem(
      "axiom.notifications.tzBannerDismissedPair.v1",
      JSON.stringify({ device: "Europe/Paris", quiet: "America/New_York" }),
    );
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "America/Los_Angeles",
      }),
    );

    renderPanel();

    await screen.findByTestId("banner-device-tz-mismatch");
  });
});

describe("NotificationPrefs Quiet Hours hint (mobile)", () => {
  it("annotates the hint with the saved zone and its current GMT offset (task #526)", async () => {
    mockApiFetch.mockResolvedValueOnce(
      makePrefs({
        quiet_hours_start_hour: 22,
        quiet_hours_end_hour: 7,
        quiet_hours_tz: "Europe/London",
      }),
    );

    renderPanel();

    // The hint includes the rendered start/end and a "(zone, GMT…)"
    // suffix. Match loosely on the offset to stay DST-resilient.
    const hint = await screen.findByText(/Mute pushes 10 PM .* 7 AM/);
    expect(hint).toHaveTextContent(/Europe\/London/);
    expect(hint).toHaveTextContent(/GMT/);
  });

  it("falls back to the generic copy when quiet hours are disabled", async () => {
    mockApiFetch.mockResolvedValueOnce(makePrefs());

    renderPanel();

    const hint = await screen.findByText(
      /Mute live-trade and halt pushes during a daily window/,
    );
    expect(hint).toBeTruthy();
  });
});
