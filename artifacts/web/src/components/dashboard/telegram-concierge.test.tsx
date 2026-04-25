import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { authState } = vi.hoisted(() => ({
  authState: {
    isSignedIn: true,
    token: "test-token" as string | null,
  },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    isSignedIn: authState.isSignedIn,
    getToken: () => Promise.resolve(authState.token),
  }),
}));

import TelegramConcierge from "./telegram-concierge";

type FetchMock = ReturnType<typeof vi.fn>;

let originalFetch: typeof fetch;
let fetchMock: FetchMock;

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function renderConcierge() {
  // Each test gets its own QueryClient so cache state never leaks
  // between cases.  Retries are off so failed responses surface
  // immediately instead of silently re-trying a few times.
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
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
  authState.isSignedIn = true;
  authState.token = "test-token";
  originalFetch = globalThis.fetch;
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
  cleanup();
});

describe("TelegramConcierge — three render states", () => {
  it("shows the loading placeholder while the status query is in flight", () => {
    // Hand the component a fetch that never resolves so we stay in
    // the initial loading state for the duration of the assertion.
    fetchMock.mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    renderConcierge();

    expect(screen.getByTestId("text-telegram-loading")).toBeTruthy();
    expect(screen.queryByTestId("button-telegram-generate-code")).toBeNull();
    expect(screen.queryByTestId("button-telegram-unlink")).toBeNull();
  });

  it("renders the linked panel with username, bound-at, and an Unlink button when /api/me/telegram reports linked=true", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        linked: true,
        telegram_user_id: 4242,
        telegram_username: "operator_jane",
        bound_at: "2026-04-24T12:00:00Z",
        last_seen_at: "2026-04-24T12:30:00Z",
        bot_username: "axiom_concierge_bot",
        bot_running: true,
      }),
    );

    renderConcierge();

    // Linked badge + handle render once the query resolves.
    await waitFor(() =>
      expect(screen.getByTestId("badge-telegram-status").textContent).toMatch(
        /linked/i,
      ),
    );
    expect(screen.getByTestId("text-telegram-username").textContent).toMatch(
      /operator_jane/,
    );
    expect(screen.getByTestId("text-telegram-bound-at").textContent).not.toBe(
      "—",
    );
    // The unlink CTA is the only action surfaced in the linked state —
    // the "Generate code" button must NOT be present here.
    expect(screen.getByTestId("button-telegram-unlink")).toBeTruthy();
    expect(screen.queryByTestId("button-telegram-generate-code")).toBeNull();
  });

  it("renders the unlinked panel with a Generate-link-code CTA and no Unlink button when /api/me/telegram reports linked=false", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        linked: false,
        bot_username: "axiom_concierge_bot",
        bot_running: true,
      }),
    );

    renderConcierge();

    await waitFor(() =>
      expect(screen.getByTestId("badge-telegram-status").textContent).toMatch(
        /not linked/i,
      ),
    );
    expect(screen.getByTestId("button-telegram-generate-code").textContent)
      .toMatch(/generate link code/i);
    expect(screen.queryByTestId("button-telegram-unlink")).toBeNull();
    // No pending code yet ⇒ no deep-link rendered.
    expect(screen.queryByTestId("link-telegram-deep-link")).toBeNull();
    expect(screen.queryByTestId("text-telegram-link-code")).toBeNull();
  });
});

describe("TelegramConcierge — Generate link code", () => {
  it("POSTs /api/me/telegram/link, renders the returned code, and builds the t.me deep-link", async () => {
    // 1) initial status load — unlinked
    // 2) POST /api/me/telegram/link — returns the code
    // 3) cache invalidation triggers another GET /api/me/telegram —
    //    still unlinked (the user hasn't sent /start to the bot yet),
    //    but now carries the pending code in the status payload.
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          linked: false,
          bot_username: "axiom_concierge_bot",
          bot_running: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          linked: false,
          bot_username: "axiom_concierge_bot",
          bot_running: true,
          pending_link_code: "ABC123",
          pending_link_expires_at: new Date(
            Date.now() + 5 * 60_000,
          ).toISOString(),
          code: "ABC123",
          expires_at: new Date(Date.now() + 5 * 60_000).toISOString(),
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          linked: false,
          bot_username: "axiom_concierge_bot",
          bot_running: true,
          pending_link_code: "ABC123",
          pending_link_expires_at: new Date(
            Date.now() + 5 * 60_000,
          ).toISOString(),
        }),
      );

    renderConcierge();

    const generateBtn = await screen.findByTestId(
      "button-telegram-generate-code",
    );
    fireEvent.click(generateBtn);

    // The mutation must hit POST /api/me/telegram/link with the auth header.
    await waitFor(() => {
      const linkCall = fetchMock.mock.calls.find(
        ([url]) => typeof url === "string" && url === "/api/me/telegram/link",
      );
      expect(linkCall).toBeTruthy();
      const init = linkCall![1] as RequestInit | undefined;
      expect(init?.method).toBe("POST");
      const headers = init?.headers as Headers | undefined;
      expect(headers?.get("authorization")).toBe("Bearer test-token");
      // The body is JSON.stringified — at minimum, an object payload.
      expect(typeof init?.body).toBe("string");
    });

    // The returned code is rendered verbatim.
    const codeEl = await screen.findByTestId("text-telegram-link-code");
    expect(codeEl.textContent).toBe("ABC123");

    // The deep-link is built as https://t.me/<bot>?start=<code> and
    // points at the bot returned by the status endpoint.
    const link = screen.getByTestId(
      "link-telegram-deep-link",
    ) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(
      "https://t.me/axiom_concierge_bot?start=ABC123",
    );
    // External link — must open in a new tab and not leak the referrer.
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toMatch(/noopener/);
  });
});

describe("TelegramConcierge — Unlink", () => {
  it("DELETEs /api/me/telegram and re-fetches status, swapping the UI back to the unlinked state", async () => {
    // 1) initial status load — linked
    // 2) DELETE /api/me/telegram — server confirms removal
    // 3) cache invalidation triggers another GET — now linked=false
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          linked: true,
          telegram_user_id: 4242,
          telegram_username: "operator_jane",
          bound_at: "2026-04-24T12:00:00Z",
          last_seen_at: "2026-04-24T12:30:00Z",
          bot_username: "axiom_concierge_bot",
          bot_running: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ok: true, removed: true }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          linked: false,
          bot_username: "axiom_concierge_bot",
          bot_running: true,
        }),
      );

    renderConcierge();

    const unlinkBtn = await screen.findByTestId("button-telegram-unlink");
    fireEvent.click(unlinkBtn);

    // The DELETE call must hit /api/me/telegram with the auth header.
    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls.find(
        ([url, init]) =>
          typeof url === "string" &&
          url === "/api/me/telegram" &&
          (init as RequestInit | undefined)?.method === "DELETE",
      );
      expect(deleteCall).toBeTruthy();
      const init = deleteCall![1] as RequestInit;
      const headers = init.headers as Headers;
      expect(headers.get("authorization")).toBe("Bearer test-token");
    });

    // After invalidation the status query re-fetches; the UI must
    // swap to the unlinked panel with the Generate-code CTA.
    await waitFor(() =>
      expect(
        screen.getByTestId("button-telegram-generate-code"),
      ).toBeTruthy(),
    );
    expect(screen.getByTestId("badge-telegram-status").textContent).toMatch(
      /not linked/i,
    );
    expect(screen.queryByTestId("button-telegram-unlink")).toBeNull();

    // And the cache invalidation actually issued at least two GETs to
    // /api/me/telegram (the initial load + the post-delete refetch).
    const statusGets = fetchMock.mock.calls.filter(
      ([url, init]) =>
        typeof url === "string" &&
        url === "/api/me/telegram" &&
        (init as RequestInit | undefined)?.method !== "DELETE",
    );
    expect(statusGets.length).toBeGreaterThanOrEqual(2);
  });
});
