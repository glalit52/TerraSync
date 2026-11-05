import clsx from "clsx";
import { Check, Minus } from "lucide-react";
import { useState } from "react";
import { useSensing, useSensingTarget } from "../api/hooks";
import { Card, Caveat, ErrorState, PageHeader, Q } from "../components/ui";
import { humanise } from "../lib/format";

const PLATFORMS = ["sentinel-2", "sentinel-1", "landsat", "planetscope", "skysat", "worldview", "uav"];

export default function Sensing() {
  const [task, setTask] = useState("detect");
  const [target, setTarget] = useState("");
  const m = useSensing(task);
  const t = useSensingTarget(task, target);
  return (
    <div className="space-y-4">
      <PageHeader
        title="Sensing limits"
        subtitle="What each platform can resolve. A target below a platform's limit is not recovered by a better model — the information is not in the data."
        actions={
          <select className="input w-auto" value={task} onChange={(e) => setTask(e.target.value)}>
            {["detect", "classify", "identify", "track"].map((x) => <option key={x} value={x}>{humanise(x)}</option>)}
          </select>
        }
      />
      <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
        <Card bodyClass="p-0">
          <Q q={m}>
            {(d) => (
              <div className="overflow-x-auto">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Target</th>
                      <th className="text-right">Smallest</th>
                      {PLATFORMS.map((p) => <th key={p} className="text-center">{p}</th>)}
                      <th>Cheapest</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.matrix.map((r) => (
                      <tr key={r.target} className={clsx("row", target === r.target && "bg-raised")} onClick={() => setTarget(r.target)}>
                        <td className="font-medium">{humanise(r.target)}</td>
                        <td className="text-right tabular-nums">{r.smallest_m} m</td>
                        {PLATFORMS.map((p) => (
                          <td key={p} className="text-center">
                            {r[p] ? <Check className="mx-auto h-4 w-4 text-accent" /> : <Minus className="mx-auto h-4 w-4 text-line" />}
                          </td>
                        ))}
                        <td className="text-xs">{r.cheapest ?? <span className="text-crit">not from orbit</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Q>
        </Card>
        <Card title={target ? `${humanise(target)} — ${task}` : "Reasoning"}>
          {!target ? (
            <p className="text-sm text-dim">Select a target to see the reasoning per platform.</p>
          ) : t.error ? (
            <ErrorState error={t.error} />
          ) : t.data ? (
            <div className="space-y-3 text-sm">
              <p className="whitespace-pre-wrap leading-relaxed">{t.data.explanation}</p>
              <div className="text-xs text-dim">Possible with: {t.data.platforms.join(", ") || "no platform"}</div>
            </div>
          ) : null}
        </Card>
      </div>
      <Caveat>People cannot be seen from orbit at any price, and drones in flight cannot either — airborne drone detection is an RF or radar problem. Satellites revisit in days, so vehicle counting per pass is a capability and tracking between passes is not.</Caveat>
    </div>
  );
}
