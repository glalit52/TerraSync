import { AlertTriangle, Satellite, Upload } from "lucide-react";
import { useMemo, useState } from "react";
import { useSites } from "../api/hooks";
import { WorldMap, type MapLine, type MapPoint } from "../components/WorldMap";
import { Card, Caveat, Empty, PageHeader, Stat, Tag } from "../components/ui";
import { fmtNum } from "../lib/format";
import { ageDays, checksumOk, groundTrack, parseTles, passes, SAMPLE_TLE, subpoint, toSat, type Pass, type Sat } from "../lib/orbit";

const iso = (d: Date) => d.toISOString().slice(0, 16);

export default function OrbitPage() {
  const sites = useSites();
  const [text, setText] = useState(SAMPLE_TLE);
  const [hours, setHours] = useState(24);
  const [minEl, setMinEl] = useState(10);
  const [startOverride, setStartOverride] = useState("");

  const { sats, errors } = useMemo(() => {
    const errs: string[] = [];
    const parsed = parseTles(text);
    const out: Sat[] = [];
    parsed.forEach((t) => {
      if (!checksumOk(t.line1) || !checksumOk(t.line2)) errs.push(`${t.name}: checksum does not match — check for a copy error`);
      try {
        const s = toSat(t);
        if (s.satrec.error) errs.push(`${t.name}: SGP4 error ${s.satrec.error}`);
        else out.push(s);
      } catch (e) {
        errs.push(`${t.name}: ${(e as Error).message}`);
      }
    });
    if (text.trim() && !parsed.length) errs.push("No two-line element sets found. Paste name + line 1 + line 2, or line 1 + line 2.");
    return { sats: out, errors: errs };
  }, [text]);

  // Propagating years past epoch is meaningless, so stale elements are
  // evaluated at their own epoch and the screen says so.
  const now = new Date();
  const stale = sats.length > 0 && sats.every((s) => Math.abs(ageDays(s, now)) > 14);
  const start = startOverride ? new Date(`${startOverride}:00Z`) : stale ? sats[0].epoch : now;

  const siteList = (sites.data ?? []).map((s) => ({ id: s.id, name: s.name, lon: s.centroid[0], lat: s.centroid[1] }));

  const allPasses = useMemo<Pass[]>(() => {
    const out: Pass[] = [];
    sats.forEach((sat) => siteList.forEach((site) => out.push(...passes(sat, site, start, hours, minEl))));
    return out.sort((a, b) => a.start.getTime() - b.start.getTime());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sats, sites.data, start.getTime(), hours, minEl]);

  const lines: MapLine[] = sats.flatMap((s) =>
    groundTrack(s, start, Math.min(hours * 60, Math.round(s.periodMin * 2) || 180)).map((seg, i) => ({ id: `${s.norad}-${i}`, coords: seg, tone: "sat" as const })),
  );
  const points: MapPoint[] = [
    ...siteList.map((s) => ({ id: s.id, lon: s.lon, lat: s.lat, tone: "site" as const, label: s.name.split(" — ")[0] })),
    ...sats.flatMap((s) => {
      const p = subpoint(s, start);
      return p ? [{ id: `sat-${s.norad}`, lon: p.lon, lat: p.lat, tone: "sat" as const, size: 7, pulse: true, label: `${s.name} · ${fmtNum(p.altKm)} km` }] : [];
    }),
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Orbit — space domain awareness"
        subtitle="Ground tracks and overflight windows over monitored sites, propagated with SGP4 from the elements you provide."
        actions={<Tag className="text-info">client-side · no network</Tag>}
      />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Objects loaded" value={sats.length} icon={<Satellite className="h-4 w-4" />} tone="info" />
        <Stat label="Overflights" value={allPasses.length} sub={`next ${hours} h, ≥ ${minEl}° elevation`} tone="accent" />
        <Stat label="Sites covered" value={new Set(allPasses.map((p) => p.site)).size} sub={`of ${siteList.length}`} />
        <Stat label="Element age" value={sats.length ? `${fmtNum(Math.min(...sats.map((s) => Math.abs(ageDays(s, now)))), 0)} d` : "—"} sub="youngest set loaded" tone={stale ? "crit" : "ink"} />
      </div>

      {stale && (
        <div className="flex gap-2 rounded-md border border-med/40 bg-med/5 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-med" />
          <div>
            Every element set loaded is more than 14 days from its epoch, so positions are computed at the epoch ({start.toISOString().slice(0, 16)}Z), not
            now. SGP4 error grows with age; paste current elements from Space-Track, CelesTrak or your own catalogue for live windows.
          </div>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[360px_1fr]">
        <Card title="Element sets">
          <div className="space-y-3">
            <textarea className="input h-44 font-mono text-[11px] leading-snug" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
            <label className="btn w-full cursor-pointer justify-center">
              <Upload className="h-4 w-4" /> Load .tle / .txt
              <input
                type="file"
                accept=".tle,.txt,text/plain"
                className="hidden"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (f) setText(await f.text());
                }}
              />
            </label>
            {errors.map((e) => <p key={e} className="text-xs text-crit">{e}</p>)}
            <div className="grid grid-cols-2 gap-2">
              <label>
                <span className="label">Window (hours)</span>
                <input type="number" min={1} max={168} className="input" value={hours} onChange={(e) => setHours(Math.max(1, Math.min(168, Number(e.target.value))))} />
              </label>
              <label>
                <span className="label">Min elevation (°)</span>
                <input type="number" min={0} max={89} className="input" value={minEl} onChange={(e) => setMinEl(Number(e.target.value))} />
              </label>
            </div>
            <label className="block">
              <span className="label">Start (UTC)</span>
              <input type="datetime-local" className="input" value={startOverride || iso(start)} onChange={(e) => setStartOverride(e.target.value)} />
            </label>
            {sats.map((s) => (
              <div key={s.norad} className="rounded-md border border-line p-2 text-xs">
                <div className="font-medium">{s.name}</div>
                <div className="text-dim">
                  NORAD {s.norad} · i {fmtNum(s.inclinationDeg, 1)}° · period {fmtNum(s.periodMin, 1)} min · epoch {s.epoch.toISOString().slice(0, 10)}
                </div>
              </div>
            ))}
          </div>
        </Card>
        <Card title="Ground tracks (two orbits)" bodyClass="p-2">
          <WorldMap height={520} lines={lines} points={points} focus={[[-180, -60], [180, 75]]} />
        </Card>
      </div>

      <Card title="Overflight windows" bodyClass="p-0">
        {allPasses.length ? (
          <div className="max-h-[420px] overflow-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Object</th>
                  <th>Site</th>
                  <th>Rise (UTC)</th>
                  <th>Peak (UTC)</th>
                  <th>Set (UTC)</th>
                  <th className="text-right">Duration</th>
                  <th className="text-right">Max elevation</th>
                </tr>
              </thead>
              <tbody>
                {allPasses.map((p, i) => (
                  <tr key={i}>
                    <td>{p.sat}</td>
                    <td className="mono">{p.site}</td>
                    <td className="mono">{p.start.toISOString().slice(5, 16).replace("T", " ")}</td>
                    <td className="mono">{p.peak.toISOString().slice(11, 16)}</td>
                    <td className="mono">{p.end.toISOString().slice(11, 16)}</td>
                    <td className="text-right">{fmtNum((p.end.getTime() - p.start.getTime()) / 60_000, 1)} min</td>
                    <td className="text-right">{fmtNum(p.maxElevationDeg, 1)}°</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="No overflights in this window">Lower the minimum elevation, lengthen the window, or load more objects.</Empty>
        )}
      </Card>
      <Caveat>
        This screen computes geometry only: when an object is above a site's horizon. Catalogue maintenance, manoeuvre detection and conjunction
        screening need the Orbit service in the PRD (FR-8) and are not built yet. Visibility is not collection: whether an overflying sensor imaged a
        site depends on its pointing and tasking, which elements do not reveal.
      </Caveat>
    </div>
  );
}
