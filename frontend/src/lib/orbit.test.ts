import { describe, expect, it } from "vitest";
import { ageDays, checksumOk, groundTrack, parseTles, passes, SAMPLE_TLE, subpoint, toSat } from "./orbit";

describe("orbit", () => {
  const [tle] = parseTles(SAMPLE_TLE);
  const sat = toSat(tle);

  it("parses a named three-line set", () => {
    expect(parseTles(SAMPLE_TLE)).toHaveLength(1);
    expect(tle.name).toMatch(/^ISS/);
  });

  it("parses bare two-line sets and names them by catalogue number", () => {
    const bare = SAMPLE_TLE.split("\n").slice(1).join("\n");
    expect(parseTles(bare)[0].name).toBe("NORAD 25544");
  });

  it("verifies checksums and catches a copy error", () => {
    expect(checksumOk(tle.line1)).toBe(true);
    expect(checksumOk(tle.line2)).toBe(true);
    expect(checksumOk(tle.line1.replace("98067A", "98068A"))).toBe(false);
  });

  it("derives epoch, period and inclination", () => {
    expect(sat.epoch.toISOString().slice(0, 10)).toBe("2019-06-05");
    expect(sat.periodMin).toBeCloseTo(92.8, 1);
    expect(sat.inclinationDeg).toBeCloseTo(51.64, 2);
  });

  it("keeps the subpoint inside the inclination band, at ISS altitude", () => {
    for (let m = 0; m < 200; m += 10) {
      const p = subpoint(sat, new Date(sat.epoch.getTime() + m * 60_000))!;
      expect(Math.abs(p.lat)).toBeLessThanOrEqual(51.7);
      expect(p.altKm).toBeGreaterThan(380);
      expect(p.altKm).toBeLessThan(440);
    }
  });

  it("splits the ground track at the antimeridian", () => {
    const segs = groundTrack(sat, sat.epoch, 190);
    segs.forEach((s) => s.slice(1).forEach((pt, i) => expect(Math.abs(pt[0] - s[i][0])).toBeLessThan(180)));
  });

  it("finds overflight windows that respect the elevation mask", () => {
    const site = { id: "IN-MUN-PORT", lon: 69.7, lat: 22.75 };
    const p = passes(sat, site, sat.epoch, 24, 10);
    expect(p.length).toBeGreaterThan(0);
    p.forEach((x) => {
      expect(x.maxElevationDeg).toBeGreaterThanOrEqual(10);
      expect(x.end.getTime()).toBeGreaterThanOrEqual(x.start.getTime());
      expect((x.end.getTime() - x.start.getTime()) / 60_000).toBeLessThan(15);
    });
  });

  it("reports element age", () => {
    expect(ageDays(sat, new Date(sat.epoch.getTime() + 3 * 86_400_000))).toBeCloseTo(3, 5);
  });
});
