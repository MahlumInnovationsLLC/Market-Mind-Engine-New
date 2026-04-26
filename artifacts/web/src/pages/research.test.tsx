import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    list: {
      data: undefined as unknown,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    },
    decide: { mutate: vi.fn(), isPending: false },
    access: { data: { is_admin: false } as unknown },
  },
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.list,
  useMutation: () => state.decide,
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@workspace/api-client-react", () => ({
  customFetch: vi.fn(async () => ({ items: [], next_cursor: null })),
  useGetMyAccess: () => state.access,
  getGetMyAccessQueryKey: () => ["my-access"],
}));

import ResearchPage from "./research";

afterEach(() => {
  cleanup();
  state.list.data = undefined;
  state.list.isLoading = false;
  state.list.isError = false;
});

describe("ResearchPage", () => {
  it("renders the page-research shell with the directive status filter when items is empty", () => {
    state.list.data = { items: [] };
    render(<ResearchPage />);
    expect(screen.getByTestId("page-research")).toBeTruthy();
    expect(screen.getByTestId("select-directive-status")).toBeTruthy();
  });

  it("renders one directive row with directive-{id} testid per item", () => {
    state.list.data = {
      items: [
        {
          id: 7,
          title: "Boost NBA halftime model",
          status: "pending",
          author: "alice",
          created_at: "2026-04-24T10:00:00Z",
        },
      ],
      next_cursor: null,
    };
    state.access.data = { is_admin: true };
    render(<ResearchPage />);
    expect(screen.getByTestId("directive-7")).toBeTruthy();
  });
});
