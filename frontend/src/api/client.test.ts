import { describe, expect, it } from "vitest";
import { qs } from "./client";

describe("qs", () => {
  it("drops empty values and encodes the rest", () => {
    expect(qs({ site: "IN-MUN-PORT", status: "", to: undefined, n: 0 })).toBe("?site=IN-MUN-PORT&n=0");
    expect(qs({ q: "a b&c" })).toBe("?q=a%20b%26c");
    expect(qs({})).toBe("");
  });
});
