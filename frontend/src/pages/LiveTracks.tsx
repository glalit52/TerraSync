import clsx from "clsx";
import { AlertTriangle, Check, ChevronsUp, Pause, Play, Radio, Rewind, Trash2, Undo2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchReplay, useCreateZone, useLive, useRetireZone, useReviewTrackAlert, useTrackAlerts, useTrackHistory } from "../api/hooks";
import type { LiveTrack, LonLat, ReplayResponse, TrackAlert, TrackKind, Zone, ZoneRule } from "../api/types";
import { WorldMap, type MapLine, type MapPoint, type MapPolygon } from "../components/WorldMap";
import { Card, Caveat, Empty, ErrorState, KeyValue, Loading, PageHeader, Stat, Tabs, Tag } from "../components/ui";
import { circleRing } from "../lib/assets";
import { fmtDateTime, fmtLonLat, fmtNum, humanise } from "../lib/format";
import { usePalette } from "../lib/theme";
import { useAuth } from "../state/auth";

// Live tracks: what ground sensors are reporting now, the geofences they are
// checked against, and the alerts those raise. Phase 1 shows each sensor's
// own tracks; one mover seen by two sensors is two tracks until fusion lands.

const KINDS: TrackKind[] = ["person", "vehicle", "drone", "unknown"];
const RULE_LABEL: Record<ZoneRule, string> = { line: "Line crossing", no_entry: "No entry", loiter: "Loitering" };
const ZONE_TONE: Record<ZoneRule, MapPolygon["tone"]> = { line: "critical", no_entry: "critical", loiter: "medium" };

const kmh = (mps: number | null) => (mps === null ? "—" : `${fmtNum(mps * 3.6, 1)} km/h`);
const ago = (s: number) => (s < 60 ? `${Math.round(s)} s ago` : `${Math.round(s / 60)} min ago`);

function KindDot({ kind }: { kind: TrackKind }) {
  const pal = usePalette();
  const c = { person: pal.high, vehicle: pal.info, drone: pal.med, unknown: pal.dim }[kind];
  return <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: c }} />;
}

function boundsOf(zones: Zone[], sensors: { lon: number | null; lat: number | null; range_m: number }[]): [[number, number], [number, number]] | null {
  const xs: number[] = [];
  const ys: number[] = [];
  // Frame on the geofences when there are any: that is where alerts happen.
  // Sensor coverage is the fallback, and stays drawn either way.
  zones.forEach((z) => z.geometry.forEach(([x, y]) => (xs.push(x), ys.push(y))));
  if (xs.length) {
    const padX = (Math.max(...xs) - Math.min(...xs)) * 0.25 || 0.01;
    const padY = (Math.max(...ys) - Math.min(...ys)) * 0.25 || 0.01;
    return [
      [Math.min(...xs) - padX, Math.min(...ys) - padY],
      [Math.max(...xs) + padX, Math.max(...ys) + padY],
    ];
  }
  sensors.forEach((s) => {
    if (s.lon === null || s.lat === null) return;
    const r = circleRing([s.lon, s.lat], Math.min(s.range_m || 1000, 8000), 8);
    r.forEach(([x, y]) => (xs.push(x), ys.push(y)));
  });
  if (!xs.length) return null;
  return [
    [Math.min(...xs), Math.min(...ys)],
    [Math.max(...xs), Math.max(...ys)],
  ];
}

// ---------------------------------------------------------------------------
// Alert queue
// ---------------------------------------------------------------------------

function AlertCard({ a, onFocus }: { a: TrackAlert; onFocus: (a: TrackAlert) => void }) {
  const review = useReviewTrackAlert();
  const { can } = useAuth();
  const [note, setNote] = useState("");
  return (
    <li className="space-y-2 px-4 py-3">
      <button className="flex w-full items-start gap-2 text-left" onClick={() => onFocus(a)}>
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-crit" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{a.summary}</div>
          <div className="text-xs text-dim">
            {fmtDateTime(a.at)} · {RULE_LABEL[a.rule]} · {a.track_id}
            {a.simulated && <span className="ml-1 text-med">· simulated</span>}
          </div>
        </div>
      </button>
      {can("finding.review") && (
        <div className="flex flex-wrap items-center gap-1.5 pl-6">
          <input className="input h-7 flex-1 py-0 text-xs" placeholder="Note (audited)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="btn btn-primary px-2 py-1 text-xs" disabled={review.isPending} onClick={() => review.mutate({ id: a.id, status: "confirmed", note })}>
            <Check className="h-3.5 w-3.5" /> Confirm
          </button>
          <button className="btn px-2 py-1 text-xs" disabled={review.isPending} onClick={() => review.mutate({ id: a.id, status: "rejected", note })}>
            <X className="h-3.5 w-3.5" /> Reject
          </button>
          <button
            className="btn px-2 py-1 text-xs"
            disabled={review.isPending || !can("finding.escalate")}
            title={can("finding.escalate") ? "" : "Escalation needs the supervisor role"}
            onClick={() => review.mutate({ id: a.id, status: "escalated", note })}
          >
            <ChevronsUp className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      {review.error ? <ErrorState error={review.error} /> : null}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Zone editor
// ---------------------------------------------------------------------------

function ZoneEditor({
  zones,
  draft,
  setDraft,
  drawing,
  setDrawing,
}: {
  zones: Zone[];
  draft: LonLat[];
  setDraft: (d: LonLat[]) => void;
  drawing: ZoneRule | null;
  setDrawing: (r: ZoneRule | null) => void;
}) {
  const create = useCreateZone();
  const retire = useRetireZone();
  const { can } = useAuth();
  const [name, setName] = useState("");
  const [kinds, setKinds] = useState<TrackKind[]>([]);
  const [loiter, setLoiter] = useState(120);
  const rule = drawing ?? "line";
  const need = rule === "line" ? 2 : 3;

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_340px]">
      <div>
        {zones.length ? (
          <table className="tbl">
            <thead>
              <tr>
                <th>Zone</th>
                <th>Rule</th>
                <th>Applies to</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {zones.map((z) => (
                <tr key={z.id}>
                  <td>
                    {z.name} {z.simulated && <Tag className="ml-1 text-med">simulated</Tag>}
                  </td>
                  <td>
                    {RULE_LABEL[z.rule]}
                    {z.rule === "loiter" ? ` > ${z.loiter_s} s` : ""}
                  </td>
                  <td className="text-xs">{z.kinds.length ? z.kinds.join(", ") : "all kinds"}</td>
                  <td className="text-right">
                    {can("zone.manage") && (
                      <button className="btn btn-ghost px-2" onClick={() => retire.mutate(z.id)} aria-label={`Retire ${z.name}`}>
                        <Trash2 className="h-4 w-4" />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty title="No zones">Draw a line or a zone on the map. Tracks are checked against every active zone as they arrive.</Empty>
        )}
      </div>
      {can("zone.manage") && (
        <div className="space-y-2">
          <div className="card-t">New zone</div>
          <div className="grid grid-cols-3 gap-1 rounded-md border border-line p-1 text-xs">
            {(Object.keys(RULE_LABEL) as ZoneRule[]).map((r) => (
              <button
                key={r}
                className={clsx("rounded px-1 py-1", drawing === r ? "bg-raised text-ink" : "text-dim")}
                onClick={() => {
                  setDrawing(r);
                  setDraft([]);
                }}
              >
                {RULE_LABEL[r]}
              </button>
            ))}
          </div>
          {drawing ? (
            <>
              <p className="text-xs text-dim">
                Click the map to place {drawing === "line" ? "the line's points" : "the zone's corners"}. {draft.length} placed, {need} needed.
              </p>
              <input className="input" placeholder="Name, e.g. Sector 7 fence" value={name} onChange={(e) => setName(e.target.value)} />
              <div className="flex flex-wrap gap-2 text-xs">
                {KINDS.map((k) => (
                  <label key={k} className="flex items-center gap-1">
                    <input type="checkbox" checked={kinds.includes(k)} onChange={(e) => setKinds((ks) => (e.target.checked ? [...ks, k] : ks.filter((x) => x !== k)))} />
                    {k}
                  </label>
                ))}
                <span className="text-dim">(none ticked = all)</span>
              </div>
              {drawing === "loiter" && (
                <label className="block">
                  <span className="label">Loiter limit (seconds)</span>
                  <input type="number" min={10} className="input" value={loiter} onChange={(e) => setLoiter(Number(e.target.value))} />
                </label>
              )}
              <div className="flex gap-2">
                <button className="btn" onClick={() => setDraft(draft.slice(0, -1))} disabled={!draft.length}>
                  <Undo2 className="h-4 w-4" />
                </button>
                <button className="btn" onClick={() => (setDrawing(null), setDraft([]))}>Cancel</button>
                <button
                  className="btn btn-primary flex-1 justify-center"
                  disabled={!name || draft.length < need || create.isPending}
                  onClick={() =>
                    create.mutate(
                      { name, rule: drawing, geometry: draft, kinds, loiter_s: drawing === "loiter" ? loiter : 0 },
                      { onSuccess: () => (setDraft([]), setDrawing(null), setName(""), setKinds([])) },
                    )
                  }
                >
                  Save zone
                </button>
              </div>
              {create.error ? <ErrorState error={create.error} /> : null}
            </>
          ) : (
            <p className="text-xs text-dim">Pick a rule, then draw on the map.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

interface ReplayState {
  data: ReplayResponse;
  t: number;
  start: number;
  end: number;
}

function replayFrame(r: ReplayState): { tracks: (LiveTrack & { trailOnly?: boolean })[] } {
  const byTrack = new Map<string, ReplayResponse["points"]>();
  r.data.points.forEach((p) => byTrack.set(p.track_id, [...(byTrack.get(p.track_id) ?? []), p]));
  const tracks: LiveTrack[] = [];
  byTrack.forEach((pts, id) => {
    const upto = pts.filter((p) => Date.parse(p.t) <= r.t);
    if (!upto.length) return;
    const last = upto[upto.length - 1];
    const age = (r.t - Date.parse(last.t)) / 1000;
    if (age > 60) return;
    tracks.push({
      id,
      uid: id,
      sensor_id: "",
      kind: last.kind,
      lon: last.lon,
      lat: last.lat,
      speed_mps: null,
      heading_deg: null,
      first_seen: pts[0].t,
      last_seen: last.t,
      points: upto.length,
      age_s: age,
      state: age <= 30 ? "live" : "lost",
      simulated: last.simulated,
      trail: upto.filter((p) => Date.parse(p.t) >= r.t - 120_000).map((p) => [p.lon, p.lat] as LonLat),
    });
  });
  return { tracks };
}

// ---------------------------------------------------------------------------

type Panel = "track" | "replay" | "zones" | "sensors" | "connect";

export default function LiveTracks() {
  const [paused, setPaused] = useState(false);
  const [replay, setReplay] = useState<ReplayState | null>(null);
  const [replayMin, setReplayMin] = useState(15);
  const [replayErr, setReplayErr] = useState<unknown>(null);
  const live = useLive(paused || !!replay);
  const pending = useTrackAlerts("pending");
  const [selected, setSelected] = useState<string | null>(null);
  const [panel, setPanel] = useState<Panel>("track");
  const [kinds, setKinds] = useState<TrackKind[]>(KINDS);
  const [showLost, setShowLost] = useState(true);
  const [drawing, setDrawing] = useState<ZoneRule | null>(null);
  const [draft, setDraft] = useState<LonLat[]>([]);
  const [flash, setFlash] = useState<TrackAlert | null>(null);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const history = useTrackHistory(historyFor);
  const lastCursor = useRef<number | null>(null);

  const d = live.data;

  // A new alert flashes a banner. The first load sets the cursor silently, so
  // opening the page does not replay yesterday's alerts as news.
  useEffect(() => {
    if (!d) return;
    if (lastCursor.current !== null && d.alert_cursor > lastCursor.current) {
      const fresh = pending.data?.find((a) => a.seq > (lastCursor.current ?? 0));
      if (fresh) setFlash(fresh);
    }
    lastCursor.current = d.alert_cursor;
  }, [d, pending.data]);

  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(null), 8000);
    return () => clearTimeout(t);
  }, [flash]);

  const focus = useMemo(() => (d ? boundsOf(d.zones, d.sensors) : null), [d ? JSON.stringify(d.zones.map((z) => z.id)) + JSON.stringify(d.sensors.map((s) => s.id)) : ""]); // eslint-disable-line react-hooks/exhaustive-deps

  const shown = useMemo(() => {
    const src = replay ? replayFrame(replay).tracks : d?.tracks ?? [];
    return src.filter((t) => kinds.includes(t.kind) && (showLost || t.state === "live"));
  }, [d, replay, kinds, showLost]);

  const sel = shown.find((t) => t.id === selected) ?? d?.tracks.find((t) => t.id === selected) ?? null;
  const simulated = (d?.sensors ?? []).some((s) => s.simulated);
  const lastPoll = live.dataUpdatedAt ? (Date.now() - live.dataUpdatedAt) / 1000 : null;
  const [, tick] = useState(0);
  useEffect(() => {
    const i = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(i);
  }, []);

  const startReplay = async () => {
    const end = Date.now();
    const start = end - replayMin * 60_000;
    setReplayErr(null);
    try {
      const data = await fetchReplay(new Date(start).toISOString(), new Date(end).toISOString());
      setReplay({ data, start, end, t: start });
    } catch (e) {
      setReplayErr(e);
    }
  };

  useEffect(() => {
    if (!replay || paused) return;
    const i = setInterval(() => setReplay((r) => (r ? { ...r, t: Math.min(r.end, r.t + 10_000) } : r)), 500);
    return () => clearInterval(i);
  }, [replay, paused]);

  if (live.isLoading) return <Loading label="Connecting to sensors" />;
  if (live.error) return <ErrorState error={live.error} />;
  if (!d) return null;

  const counts = KINDS.map((k) => ({ k, n: d.tracks.filter((t) => t.kind === k && t.state === "live").length }));
  const online = d.sensors.filter((s) => s.status === "online").length;

  const polygons: MapPolygon[] = [
    ...d.sensors
      .filter((s) => s.lon !== null && s.lat !== null && s.range_m > 0)
      .map((s) => ({ id: `cov-${s.id}`, ring: circleRing([s.lon!, s.lat!], s.range_m, 64), tone: "draft" as const, label: `${s.name} coverage (${fmtNum(s.range_m)} m)` })),
    ...d.zones.filter((z) => z.rule !== "line").map((z) => ({ id: z.id, ring: z.geometry, tone: ZONE_TONE[z.rule], label: `${z.name} · ${RULE_LABEL[z.rule]}` })),
    ...(drawing && drawing !== "line" && draft.length >= 3 ? [{ id: "draft", ring: draft, tone: "draft" as const }] : []),
  ];
  const lines: MapLine[] = [
    ...d.zones.filter((z) => z.rule === "line").map((z) => ({ id: z.id, coords: z.geometry, tone: "critical" as const, dashed: false, width: 2.5 })),
    ...shown.filter((t) => t.trail.length > 1).map((t) => ({ id: `tr-${t.id}`, coords: t.trail, tone: t.kind, dashed: false, width: t.id === selected ? 2.5 : 1.4, faded: t.state === "lost" })),
    ...(drawing === "line" && draft.length >= 2 ? [{ id: "draft-line", coords: draft, tone: "sat" as const }] : []),
    ...(historyFor && history.data ? [{ id: "hist", coords: history.data.points.map((p) => [p.lon, p.lat] as LonLat), tone: "accent" as const, width: 2 }] : []),
  ];
  const points: MapPoint[] = [
    ...d.sensors.filter((s) => s.lon !== null).map((s) => ({ id: s.id, lon: s.lon!, lat: s.lat!, tone: "sensor" as const, size: 5, label: `${s.name} · ${s.status}` })),
    ...shown.map((t) => ({
      id: t.id,
      lon: t.lon,
      lat: t.lat,
      tone: t.kind,
      size: t.kind === "unknown" ? 3.5 : 5,
      pulse: t.state === "live" && pending.data?.some((a) => a.track_id === t.id),
      faded: t.state === "lost",
      label: `${t.kind} · ${t.uid} · ${kmh(t.speed_mps)}${t.state === "lost" ? " · lost" : ""}`,
    })),
    ...draft.map((p, i) => ({ id: `v${i}`, lon: p[0], lat: p[1], tone: "sat" as const, size: 3 })),
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Live tracks"
        subtitle="Movers reported by ground sensors, checked against geofences as they arrive. Every alert waits for an operator."
        actions={
          <>
            <span className={clsx("chip px-2 py-1", replay ? "border-info/50 text-info" : paused ? "border-med/50 text-med" : "border-accent/50 text-accent")}>
              <Radio className={clsx("h-3.5 w-3.5", !paused && !replay && "animate-pulse")} />
              {replay ? "REPLAY" : paused ? "PAUSED" : "LIVE"}
              {!replay && lastPoll !== null && <span className="text-dim"> · updated {Math.round(lastPoll)} s ago</span>}
            </span>
            <button className="btn" onClick={() => setPaused((p) => !p)}>
              {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />} {paused ? "Resume" : "Pause"}
            </button>
          </>
        }
      />

      {simulated && (
        <div className="flex gap-2 rounded-md border border-med/40 bg-med/5 p-3 text-sm text-med">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          A simulated sensor is connected. Its tracks, its fence line and the alerts they raise are invented to demonstrate the workflow; none is a real
          observation or a real boundary.
        </div>
      )}
      {flash && (
        <button className="flex w-full items-center gap-2 rounded-md border border-crit/50 bg-crit/10 p-3 text-left text-sm text-crit" onClick={() => (setSelected(flash.track_id), setPanel("track"))}>
          <AlertTriangle className="h-4 w-4 animate-pulse" /> New alert: {flash.summary} · {fmtDateTime(flash.at)}
          {flash.simulated && <span className="text-med"> (simulated)</span>}
        </button>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        {counts.map(({ k, n }) => (
          <Stat key={k} label={`${humanise(k)}s live`} value={n} tone={k === "person" ? "high" : k === "vehicle" ? "info" : k === "drone" ? "med" : "ink"} />
        ))}
        <Stat label="Sensors online" value={`${online} / ${d.sensors.length}`} tone={online < d.sensors.length ? "med" : "accent"} />
        <Stat label="Alerts pending" value={d.pending_alerts} tone={d.pending_alerts ? "crit" : "ink"} />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
        <Card bodyClass="p-2">
          <div className="mb-2 flex flex-wrap items-center gap-3 px-2 text-xs">
            {KINDS.map((k) => (
              <label key={k} className="flex items-center gap-1.5">
                <input type="checkbox" checked={kinds.includes(k)} onChange={(e) => setKinds((ks) => (e.target.checked ? [...ks, k] : ks.filter((x) => x !== k)))} />
                <KindDot kind={k} /> {k}
              </label>
            ))}
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={showLost} onChange={(e) => setShowLost(e.target.checked)} /> show lost (&gt; {d.lost_after_s} s silent)
            </label>
            <span className="ml-auto text-dim">red line/zone = geofence · circle = sensor coverage</span>
          </div>
          {d.sensors.length === 0 && !d.zones.length ? (
            <Empty title="No sensors connected">Open the Connect tab below to feed the simulated radar or a real sensor gateway.</Empty>
          ) : (
            <WorldMap
              height={560}
              labels={false}
              focus={focus}
              selected={selected}
              onSelect={(id) => {
                setSelected(id);
                setPanel("track");
              }}
              drawing={!!drawing}
              onDrawPoint={(p) => setDraft((x) => [...x, p])}
              polygons={polygons}
              lines={lines}
              points={points}
            />
          )}
          {replay && (
            <div className="mt-2 flex items-center gap-3 px-2">
              <Rewind className="h-4 w-4 text-info" />
              <input
                type="range"
                min={replay.start}
                max={replay.end}
                step={2000}
                value={replay.t}
                onChange={(e) => setReplay({ ...replay, t: Number(e.target.value) })}
                className="flex-1"
                aria-label="Replay time"
              />
              <span className="font-mono text-xs">{new Date(replay.t).toISOString().slice(11, 19)}Z</span>
              <button className="btn text-xs" onClick={() => setReplay(null)}>Back to live</button>
            </div>
          )}
        </Card>

        <Card title={`Alert queue (${pending.data?.length ?? 0})`} bodyClass="p-0" className="max-h-[640px]">
          {pending.data?.length ? (
            <ul className="h-full divide-y divide-line overflow-y-auto">
              {pending.data.map((a) => (
                <AlertCard key={a.id} a={a} onFocus={(x) => (setSelected(x.track_id), setPanel("track"))} />
              ))}
            </ul>
          ) : (
            <Empty title="No alerts waiting">Line crossings, no-entry entries and loitering appear here within a sweep.</Empty>
          )}
        </Card>
      </div>

      <Tabs<Panel>
        value={panel}
        onChange={setPanel}
        tabs={[
          { id: "track", label: "Selected track" },
          { id: "replay", label: "Replay" },
          { id: "zones", label: `Zones (${d.zones.length})` },
          { id: "sensors", label: `Sensors (${d.sensors.length})` },
          { id: "connect", label: "Connect sensors" },
        ]}
      />

      {panel === "track" && (
        <Card>
          {sel ? (
            <div className="grid gap-4 md:grid-cols-2">
              <KeyValue
                rows={[
                  ["Track", <span key="t" className="mono">{sel.uid} · {sel.id}</span>],
                  ["Kind", <span key="k" className="flex items-center gap-1.5"><KindDot kind={sel.kind} /> {sel.kind} (as classified by the sensor)</span>],
                  ["State", sel.state === "live" ? `live, last fix ${ago(sel.age_s)}` : `lost, last fix ${ago(sel.age_s)}`],
                  ["Position", <span key="p" className="mono">{fmtLonLat(sel.lon, sel.lat)}</span>],
                  ["Speed / heading", `${kmh(sel.speed_mps)} · ${sel.heading_deg === null ? "—" : `${Math.round(sel.heading_deg)}°`}`],
                  ["First seen", fmtDateTime(sel.first_seen)],
                  ["Fixes", sel.points],
                  ["Sensor", sel.sensor_id || "—"],
                ]}
              />
              <div className="space-y-2 text-sm">
                <button className="btn" onClick={() => setHistoryFor(historyFor === sel.id ? null : sel.id)}>
                  {historyFor === sel.id ? "Hide full history" : "Show full history on map"}
                </button>
                {historyFor === sel.id && history.data && <p className="text-xs text-dim">{history.data.points.length} fixes since {fmtDateTime(history.data.first_seen)}. This read is recorded in the audit log.</p>}
                {history.error ? <ErrorState error={history.error} /> : null}
                {sel.simulated && <p className="text-xs text-med">Simulated track.</p>}
                <Caveat>A track is a sensor's identifier for something moving. It carries no identity, owner or affiliation, and the kind is the sensor's classification.</Caveat>
              </div>
            </div>
          ) : (
            <p className="text-sm text-dim">Click a track on the map, or an alert in the queue.</p>
          )}
        </Card>
      )}

      {panel === "replay" && (
        <Card>
          <div className="flex flex-wrap items-end gap-3">
            <label>
              <span className="label">Replay the last</span>
              <select className="input w-auto" value={replayMin} onChange={(e) => setReplayMin(Number(e.target.value))}>
                {[5, 15, 30, 60, 180].map((m) => <option key={m} value={m}>{m} minutes</option>)}
              </select>
            </label>
            <button className="btn btn-primary" onClick={startReplay}>
              <Rewind className="h-4 w-4" /> Load replay
            </button>
            {replay && <span className="text-xs text-dim">{replay.data.points.length} fixes loaded{replay.data.truncated ? " (truncated at 50,000)" : ""} · plays at 20× · Pause stops it</span>}
          </div>
          {replayErr ? <ErrorState error={replayErr} /> : null}
          <p className="mt-3 text-xs text-dim">Replays read from stored fixes, which are kept for {d.retention_hours} hours. Each replay is recorded in the audit log.</p>
        </Card>
      )}

      {panel === "zones" && (
        <Card>
          <ZoneEditor zones={d.zones} draft={draft} setDraft={setDraft} drawing={drawing} setDrawing={setDrawing} />
        </Card>
      )}

      {panel === "sensors" && (
        <Card bodyClass="p-0">
          {d.sensors.length ? (
            <table className="tbl">
              <thead>
                <tr>
                  <th>Sensor</th>
                  <th>Kind</th>
                  <th>Status</th>
                  <th>Last heard</th>
                  <th className="text-right">Fixes received</th>
                  <th className="text-right">Range</th>
                </tr>
              </thead>
              <tbody>
                {d.sensors.map((s) => (
                  <tr key={s.id}>
                    <td>
                      {s.name} <span className="mono text-dim">{s.id}</span> {s.simulated && <Tag className="ml-1 text-med">simulated</Tag>}
                    </td>
                    <td>{humanise(s.kind)}</td>
                    <td className={s.status === "online" ? "text-accent" : "text-med"}>{s.status}</td>
                    <td className="whitespace-nowrap">{fmtDateTime(s.last_heard_at)}</td>
                    <td className="text-right">{fmtNum(s.reports)}</td>
                    <td className="text-right">{s.range_m ? `${fmtNum(s.range_m)} m` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty title="No sensors registered" />
          )}
        </Card>
      )}

      {panel === "connect" && (
        <Card>
          <div className="space-y-4 text-sm">
            <div>
              <div className="card-t mb-1">Try it with the simulated radar</div>
              <pre className="overflow-x-auto rounded-md border border-line bg-bg p-3 font-mono text-[12px]">terrashield simulate-tracks --url http://127.0.0.1:8787 --token $TERRASHIELD_TOKEN</pre>
              <p className="mt-1 text-xs text-dim">
                Registers a simulated radar and three simulated zones over the Rann of Kutch demo area, then reports a patrol vehicle, two people
                crossing a fence line, a vehicle loitering, a drone and radar clutter every two seconds. Everything it creates is flagged simulated.
              </p>
            </div>
            <div>
              <div className="card-t mb-1">Feed a real sensor gateway</div>
              <p className="text-xs text-dim">
                Register the sensor once (<span className="mono">POST /api/sensors</span>), then post batches to <span className="mono">POST /api/tracks/ingest</span> as
                JSON reports or Cursor-on-Target events. Use a service token with the analyst role.
              </p>
              <pre className="mt-2 overflow-x-auto rounded-md border border-line bg-bg p-3 font-mono text-[11px] leading-relaxed">{`{"sensor_id": "radar-7",
 "reports": [{"uid": "T042", "kind": "person", "lon": 70.51, "lat": 23.92,
              "time": "2026-11-04T02:14:07Z", "speed_mps": 1.4, "heading_deg": 12}],
 "cot": ["<event uid='T043' type='a-u-G-E-V' time='...'><point lat='23.92' lon='70.52'/></event>"]}`}</pre>
            </div>
            <Caveat>
              Phase 1 accepts tracks a sensor has already formed. ASTERIX radar output needs a converter to this format; fusing one mover seen by two
              sensors into one track, and video detection at the tower, are later phases. CoT affiliation (friend, hostile) is dropped on intake: it is
              a judgement made upstream that this system cannot check.
            </Caveat>
          </div>
        </Card>
      )}
    </div>
  );
}
