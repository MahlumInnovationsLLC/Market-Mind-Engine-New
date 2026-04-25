/**
 * Component tests for the mobile Telegram concierge card (task #414).
 *
 * Locks down the binding-status branches and the link/unlink actions
 * so a regression that, e.g., stops surfacing the deep-link or hides
 * the unlink button after a successful bind surfaces here instead of
 * stranding mobile-first operators in chat-less limbo.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import React from "react";
import { Alert, Linking } from "react-native";

const mockApiFetch = jest.fn();

jest.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    body: unknown;
    requestId: string;
    constructor(
      message: string,
      status: number,
      body: unknown,
      requestId: string,
    ) {
      super(`${message} [request_id=${requestId}]`);
      this.status = status;
      this.body = body;
      this.requestId = requestId;
    }
  }
  return {
    ApiError,
    apiFetch: (...args: unknown[]) => mockApiFetch(...args),
  };
});

jest.mock("@/hooks/useColors", () => ({
  useColors: () => ({
    background: "#000",
    foreground: "#fff",
    card: "#111",
    border: "#222",
    primary: "#0af",
    primaryForeground: "#fff",
    mutedForeground: "#888",
    destructive: "#f00",
    success: "#0f0",
  }),
}));

import { TelegramConcierge } from "@/components/TelegramConcierge";

function renderCard() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <TelegramConcierge />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockApiFetch.mockReset();
  jest.spyOn(Alert, "alert").mockImplementation(() => undefined);
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe("TelegramConcierge", () => {
  it("shows the linked state with bound/last-seen timestamps", async () => {
    mockApiFetch.mockResolvedValueOnce({
      linked: true,
      telegram_user_id: 42,
      telegram_username: "operator",
      bound_at: "2026-04-20T10:00:00Z",
      last_seen_at: "2026-04-23T09:30:00Z",
      bot_username: "axiom_bot",
      bot_running: true,
    });
    renderCard();

    expect(await screen.findByTestId("badge-telegram-status")).toHaveTextContent(
      "LINKED",
    );
    expect(screen.getByTestId("text-telegram-username")).toHaveTextContent(
      "@operator",
    );
    expect(screen.getByTestId("badge-telegram-bot-status")).toHaveTextContent(
      "@axiom_bot ONLINE",
    );
    expect(screen.getByTestId("button-telegram-unlink")).toBeTruthy();
  });

  it("renders the link CTA when not bound and surfaces the new code + deep-link on success", async () => {
    mockApiFetch.mockResolvedValueOnce({
      linked: false,
      bot_username: "axiom_bot",
      bot_running: true,
    });
    renderCard();

    const generate = await screen.findByTestId("button-telegram-generate-code");
    expect(generate).toHaveTextContent("GENERATE LINK CODE");
    expect(screen.getByTestId("badge-telegram-status")).toHaveTextContent(
      "NOT LINKED",
    );

    mockApiFetch.mockResolvedValueOnce({
      linked: false,
      code: "ABC123",
      expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      bot_username: "axiom_bot",
      bot_running: true,
    });
    // Re-fetch after the mutation invalidates the status query.
    mockApiFetch.mockResolvedValueOnce({
      linked: false,
      pending_link_code: "ABC123",
      pending_link_expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
      bot_username: "axiom_bot",
      bot_running: true,
    });

    fireEvent.press(generate);

    expect(await screen.findByTestId("text-telegram-link-code")).toHaveTextContent(
      "ABC123",
    );
    const deepLink = screen.getByTestId("link-telegram-deep-link");

    const openSpy = jest
      .spyOn(Linking, "openURL")
      .mockResolvedValue(true as unknown as void);
    fireEvent.press(deepLink);
    expect(openSpy).toHaveBeenCalledWith(
      "https://t.me/axiom_bot?start=ABC123",
    );

    await waitFor(() =>
      expect(screen.getByTestId("button-telegram-generate-code")).toHaveTextContent(
        "REGENERATE CODE",
      ),
    );
  });

  it("confirms before unlinking and calls DELETE /api/me/telegram on confirm", async () => {
    mockApiFetch.mockResolvedValueOnce({
      linked: true,
      telegram_user_id: 42,
      telegram_username: "operator",
      bound_at: "2026-04-20T10:00:00Z",
      last_seen_at: "2026-04-23T09:30:00Z",
      bot_username: "axiom_bot",
      bot_running: true,
    });
    const alertSpy = jest
      .spyOn(Alert, "alert")
      .mockImplementation((_t, _m, buttons) => {
        const confirm = (buttons ?? []).find((b) => b.text === "Unlink");
        confirm?.onPress?.();
      });
    renderCard();

    const unlink = await screen.findByTestId("button-telegram-unlink");

    mockApiFetch.mockResolvedValueOnce({ ok: true, removed: true });
    mockApiFetch.mockResolvedValueOnce({
      linked: false,
      bot_username: "axiom_bot",
      bot_running: true,
    });

    fireEvent.press(unlink);

    expect(alertSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(mockApiFetch).toHaveBeenCalledWith("/api/me/telegram", {
        method: "DELETE",
      }),
    );
    await screen.findByTestId("button-telegram-generate-code");
  });

  it("renders the API error message when /api/me/telegram fails", async () => {
    const { ApiError } = jest.requireMock("@/lib/api");
    mockApiFetch.mockRejectedValueOnce(
      new ApiError("HTTP 500 :: /api/me/telegram", 500, null, "abc123"),
    );
    renderCard();

    const err = await screen.findByTestId("text-telegram-error");
    expect(err).toHaveTextContent(/Failed to load Telegram status \(HTTP 500\)/);
    expect(err).toHaveTextContent(/request_id=abc123/);
  });
});
