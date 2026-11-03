import { describe, expect, it } from "vitest";
import type { Change } from "../api/types";
import { distanceM, distanceToChange, parseAssetsCsv, pointInPolygon, SAMPLE_PORTFOLIO, screenAsset, toCsv } from "./assets";

const square: [number, number][] = [
  [69.70, 22.74],
  [69.71, 22.74],
  [69.71, 22.75],
  [69.70, 22.75],
];

function change(over: Partial<Change> = {}): Change {
  return {
    id: "chg-1", aoi_id: "IN-MUN-PORT", type: "new_structure", detected_on: "2026-05-01",
    area_m2: 2000, confidence: 0.9, severity: "high", explanation: "",
    geometry: [[69.7050, 22.7450], [69.7052, 22.7450], [69.7052, 22.7452], [69.7050, 22.7452]],
    before_scene_id: "a", after_scene_id: "b", evidence_id: "ev", review_status: "pending",
    ...over,
  };
}

describe("geometry", () => {
  it("measures a degree of latitude as about 111 km", () => {
    expect(distanceM([0, 0], [0, 1])).toBeGreaterThan(110_500);
    expect(distanceM([0, 0], [0, 1])).toBeLessThan(111_500);
  });
  it("tests containment", () => {
    expect(pointInPolygon([69.705, 22.745], square)).toBe(true);
    expect(pointInPolygon([69.72, 22.745], square)).toBe(false);
  });
  it("puts a point inside a finding at distance zero", () => {
    expect(distanceToChange([69.7051, 22.7451], change())).toBe(0);
    expect(distanceToChange([69.7100, 22.7451], change())).toBeGreaterThan(400);
  });
});

describe("parseAssetsCsv", () => {
  it("finds columns by name, keeps extras and reports bad rows", () => {
    const r = parseAssetsCsv("ID,Asset,Longitude,Latitude,TIV,Owner\nX1,Yard,69.7,22.74,\"1,200\",Acme\nX2,Bad,abc,22\n");
    expect(r.assets).toHaveLength(1);
    expect(r.assets[0]).toMatchObject({ id: "X1", name: "Yard", lon: 69.7, lat: 22.74, value: 1200, owner: "Acme" });
    expect(r.errors[0]).toMatch(/Row 3/);
  });
  it("refuses a file with no coordinate columns", () => {
    expect(parseAssetsCsv("name,city\na,b").errors[0]).toMatch(/longitude\/latitude/);
  });
  it("parses the shipped sample completely", () => {
    const r = parseAssetsCsv(SAMPLE_PORTFOLIO);
    expect(r.errors).toEqual([]);
    expect(r.assets.length).toBe(8);
  });
});

describe("screenAsset", () => {
  const asset = { id: "a", name: "A", lon: 69.7051, lat: 22.7451 };
  it("scores zero with nothing nearby", () => {
    const s = screenAsset(asset, [], 500);
    expect(s.score).toBe(0);
    expect(s.worst).toBeNull();
  });
  it("ignores rejected findings", () => {
    expect(screenAsset(asset, [change({ review_status: "rejected" })], 500).count).toBe(0);
  });
  it("weights confirmed, closer and more severe findings higher", () => {
    const base = screenAsset(asset, [change()], 500).score;
    expect(screenAsset(asset, [change({ review_status: "confirmed" })], 500).score).toBeGreaterThan(base);
    expect(screenAsset(asset, [change({ severity: "critical" })], 500).score).toBeGreaterThan(base);
    const far = { ...asset, lon: 69.7090 };
    expect(screenAsset(far, [change()], 500).score).toBeLessThan(base);
  });
  it("stays within 0..100 however many findings", () => {
    const many = Array.from({ length: 200 }, (_, i) => change({ id: `c${i}`, severity: "critical" }));
    expect(screenAsset(asset, many, 500).score).toBeLessThanOrEqual(100);
  });
});

describe("toCsv", () => {
  it("quotes fields that need it", () => {
    expect(toCsv([{ a: 'x,"y"', b: 1 }])).toBe('a,b\n"x,""y""",1');
  });
});
