/**
 * Unit tests for the `cn` className helper.
 *
 * `cn` is one of the most-imported helpers in this app — every shadcn
 * primitive and almost every page component composes class strings
 * through it. The implementation is a one-liner around `clsx` +
 * `tailwind-merge`, but the *behaviour* it inherits (truthy filtering,
 * tailwind conflict resolution) is what callers actually depend on, so
 * exercising those branches here pins it down.
 */
import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn", () => {
  it("joins simple string class names with a single space", () => {
    expect(cn("foo", "bar")).toBe("foo bar");
  });

  it("filters out falsy values (undefined / null / false / empty string)", () => {
    expect(cn("foo", undefined, null, false, "", "bar")).toBe("foo bar");
  });

  it("expands conditional class objects", () => {
    expect(cn("base", { active: true, disabled: false })).toBe("base active");
  });

  it("flattens nested arrays of class values", () => {
    expect(cn(["a", ["b", { c: true, d: false }]], "e")).toBe("a b c e");
  });

  it("resolves tailwind padding conflicts so the last value wins", () => {
    // `tailwind-merge` is the entire reason this helper exists instead of
    // bare `clsx` — earlier conflicting utility classes must be dropped
    // so the caller's intent (rightmost wins) actually applies.
    expect(cn("p-2", "p-4")).toBe("p-4");
  });

  it("resolves tailwind text-color conflicts the same way", () => {
    expect(cn("text-red-500", "text-blue-500")).toBe("text-blue-500");
  });

  it("preserves non-conflicting tailwind classes alongside conflict winners", () => {
    expect(cn("flex items-center p-2", "p-4 gap-2")).toBe(
      "flex items-center p-4 gap-2",
    );
  });

  it("returns an empty string when no truthy classes are passed", () => {
    expect(cn()).toBe("");
    expect(cn(undefined, null, false)).toBe("");
  });
});
