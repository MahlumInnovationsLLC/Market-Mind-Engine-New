import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";

const { state, setMutate } = vi.hoisted(() => ({
  state: { data: undefined as unknown, isLoading: false },
  setMutate: vi.fn(async () => ({ mode: "pipeline" as const })),
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetEdgeTerminalInputModeQueryKey: () => ["edge-terminal-input-mode"],
  useGetEdgeTerminalInputMode: () => state,
  useSetEdgeTerminalInputMode: () => ({
    mutateAsync: setMutate,
    isPending: false,
  }),
}));
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: () => undefined }),
}));

import InputModeToggle from "./input-mode-toggle";

afterEach(() => {
  cleanup();
  state.data = undefined;
  setMutate.mockClear();
});

describe("InputModeToggle", () => {
  it("highlights the active mode and offers the pipeline-data toggle in edge_terminal mode", () => {
    state.data = { mode: "edge_terminal", valid_modes: ["pipeline", "edge_terminal"] };
    render(
      <InputModeToggle showPipelineData={false} onTogglePipelineData={() => {}} />,
    );
    expect(screen.getByTestId("input-mode-edge-terminal").className)
      .toMatch(/accent-cyan/);
    expect(screen.getByTestId("show-pipeline-data-toggle")).toBeTruthy();
  });

  it("hides the pipeline-data toggle when in pipeline mode", () => {
    state.data = { mode: "pipeline", valid_modes: ["pipeline", "edge_terminal"] };
    render(
      <InputModeToggle showPipelineData={false} onTogglePipelineData={() => {}} />,
    );
    expect(screen.queryByTestId("show-pipeline-data-toggle")).toBeNull();
  });

  it("calls mutateAsync with the chosen mode on click", () => {
    state.data = { mode: "edge_terminal", valid_modes: ["pipeline", "edge_terminal"] };
    render(
      <InputModeToggle showPipelineData={false} onTogglePipelineData={() => {}} />,
    );
    fireEvent.click(screen.getByTestId("input-mode-pipeline"));
    expect(setMutate).toHaveBeenCalledWith({ data: { mode: "pipeline" } });
  });

  it("does not re-issue mutate when the chosen mode is already active", () => {
    state.data = { mode: "pipeline", valid_modes: ["pipeline", "edge_terminal"] };
    render(
      <InputModeToggle showPipelineData={false} onTogglePipelineData={() => {}} />,
    );
    fireEvent.click(screen.getByTestId("input-mode-pipeline"));
    expect(setMutate).not.toHaveBeenCalled();
  });
});
