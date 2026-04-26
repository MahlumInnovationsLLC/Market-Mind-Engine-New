import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import NotFound from "./not-found";

afterEach(() => cleanup());

describe("NotFound", () => {
  it("renders the 404 heading and the developer hint copy", () => {
    render(<NotFound />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(
      /404 page not found/i,
    );
    expect(screen.getByText(/forget to add the page to the router/i))
      .toBeTruthy();
  });
});
