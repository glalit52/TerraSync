import { describe, expect, it } from "vitest";
import type { Change } from "../api/types";
import { screenAsset } from "./assets";
import { parseCoordinate, searchPlaces } from "./gazetteer";
import { assess, distanceToLinesM, layerFromGeoJson, syntheticDemoLayers } from "./hazards";
import { recommend, STALE_DAYS } from "./inspection";

const zoneGeo = JSON.stringify({
  type: "FeatureCollection",
  features: [
    { type: "Feature", properties: { zone: "High" }, geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]]] } },
    { type: "Feature", properties: { zone: "X" }, geometry: { type: "Polygon", coordinates: [[[2, 0], [3, 0], [3, 1], [2, 1], [2, 0]]] } },
    { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [5, 5] } },
  ],
});

describe("hazard layers", () => {
  const meta = { name: "Flood", kind: "flood" as const, mode: "zone" as const, classField: "zone", source: "test" };

  it("reads zone classes, honours holes and counts skipped geometry", () => {
    const { layer, skipped } = layerFromGeoJson(zoneGeo, meta);
    expect(skipped).toBe(1);
    expect(assess([0.2, 0.2], layer!)).toMatchObject({ value: "High", flagged: true });
    expect(assess([0.5, 0.5], layer!).value).toBe("outside mapped zones");
    expect(assess([2.5, 0.5], layer!)).toMatchObject({ value: "X", flagged: false });
    expect(assess([9, 9], layer!).flagged).toBe(false);
  });

  it("refuses a zone layer with no polygons, and bad JSON", () => {
    const lines = JSON.stringify({ type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[0, 0], [1, 1]] } }] });
    expect(layerFromGeoJson(lines, meta).error).toMatch(/Polygon/);
    expect(layerFromGeoJson("{", meta).error).toMatch(/JSON/);
  });

  it("measures distance to a line segment, not just its vertices", () => {
    // A point 0.1° north of the middle of a long east-west segment at the equator: ~11.1 km.
    const d = distanceToLinesM([0.5, 0.1], [[[0, 0], [1, 0]]]);
    expect(d).toBeGreaterThan(11_000);
    expect(d).toBeLessThan(11_200);
  });

  it("marks the demo layers as synthetic", () => {
    expect(syntheticDemoLayers().every((l) => l.synthetic && /Synthetic/.test(l.source))).toBe(true);
  });
});

describe("coordinates and places", () => {
  it("reads lat,lon in the order people paste it", () => {
    expect(parseCoordinate("22.743, 69.703")).toEqual([69.703, 22.743]);
    expect(parseCoordinate("69.703 22.743")).toEqual([22.743, 69.703]);
  });
  it("swaps when the first value cannot be a latitude", () => {
    expect(parseCoordinate("120.5, 30.1")).toEqual([120.5, 30.1]);
  });
  it("reads hemispheres and degrees-minutes-seconds", () => {
    expect(parseCoordinate("22.743N 69.703E")).toEqual([69.703, 22.743]);
    const p = parseCoordinate(`22°44'30"N 69°42'0"E`)!;
    expect(p[0]).toBeCloseTo(69.7, 6);
    expect(p[1]).toBeCloseTo(22.741667, 5);
    expect(parseCoordinate("33.9S 18.4E")).toEqual([18.4, -33.9]);
  });
  it("rejects what is not a coordinate", () => {
    expect(parseCoordinate("Mundra port")).toBeNull();
    expect(parseCoordinate("95, 200")).toBeNull();
  });
  it("matches known names on every term", () => {
    const known = [{ label: "Mundra Port — container terminal", kind: "site" as const, lonlat: [69.7, 22.74] as [number, number], ref: "IN-MUN-PORT" }];
    expect(searchPlaces("mundra term", known)).toHaveLength(1);
    expect(searchPlaces("mundra airport", known)).toHaveLength(0);
  });
});

function change(over: Partial<Change> = {}): Change {
  return {
    id: "c", aoi_id: "S", type: "new_structure", detected_on: "2026-05-01", area_m2: 2000, confidence: 0.9,
    severity: "high", explanation: "", geometry: [[0, 0], [0.0002, 0], [0.0002, 0.0002], [0, 0.0002]],
    before_scene_id: "a", after_scene_id: "b", evidence_id: "e", review_status: "pending", ...over,
  };
}

describe("inspection recommendation", () => {
  const asset = { id: "a", name: "A", lon: 0.0001, lat: 0.0001 };

  it("says not observed when no site covers the asset, whatever else is true", () => {
    const r = recommend({ screening: screenAsset(asset, [], 500), hazards: [], monitored: false, daysSinceLook: null });
    expect(r.action).toBe("Not observed");
  });
  it("is no action for a recently seen, quiet, unexposed asset", () => {
    const r = recommend({ screening: screenAsset(asset, [], 500), hazards: [], monitored: true, daysSinceLook: 3 });
    expect(r.action).toBe("No action");
  });
  it("flags a stale look for desk review", () => {
    const r = recommend({ screening: screenAsset(asset, [], 500), hazards: [], monitored: true, daysSinceLook: STALE_DAYS + 1 });
    expect(r.action).toBe("Desk review");
  });
  it("sends a serious change on the asset to inspection, naming the nearest one", () => {
    const r = recommend({ screening: screenAsset(asset, [change()], 500), hazards: [], monitored: true, daysSinceLook: 3 });
    expect(r.action).toBe("Inspect");
    expect(r.reasons.join(" ")).toMatch(/high severity change on the asset/);
  });
  it("counts flagged hazards, and says when a layer is synthetic", () => {
    const flood = syntheticDemoLayers()[0];
    const r = recommend({
      screening: screenAsset(asset, [], 500),
      hazards: [{ layer: flood, value: "High", flagged: true }],
      monitored: true,
      daysSinceLook: 3,
    });
    expect(r.action).toBe("Desk review");
    expect(r.reasons[0]).toMatch(/synthetic layer/);
  });
});
