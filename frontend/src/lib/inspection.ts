import type { Screening } from "./assets";
import type { HazardResult } from "./hazards";

// Which assets are worth sending a person to. The recommendation is a
// checklist a reviewer can read, not a model: each reason is one fact from the
// record, and the action follows from how many and which reasons apply.

export type Action = "Inspect" | "Desk review" | "No action" | "Not observed";

export interface Recommendation {
  action: Action;
  reasons: string[];
  /** For sorting a queue: higher first. */
  priority: number;
}

/** Days after which a site's last usable look is too old to vouch for "no change". */
export const STALE_DAYS = 45;

export function recommend(input: {
  screening: Screening;
  hazards: HazardResult[];
  monitored: boolean;
  /** Days since the covering site's last usable acquisition; null if none in the window. */
  daysSinceLook: number | null;
  /** Asset value relative to the portfolio's median value, when values are known. */
  valueRatio?: number;
}): Recommendation {
  const { screening: s, hazards, monitored, daysSinceLook, valueRatio } = input;
  const reasons: string[] = [];
  let points = 0;

  const flagged = hazards.filter((h) => h.flagged);
  flagged.forEach((h) => {
    reasons.push(`${h.layer.name}: ${h.value}${h.layer.synthetic ? " (synthetic layer)" : ""}`);
    points += 1;
  });

  if (!monitored) {
    reasons.unshift("No satellite monitoring covers this asset: change cannot be ruled out");
    return { action: "Not observed", reasons, priority: 50 + points * 5 };
  }

  // `nearby` is sorted nearest first, so this is the closest serious change.
  const serious = s.nearby.find((x) => x.change.severity === "critical" || x.change.severity === "high");
  if (serious) {
    const where = serious.distanceM === 0 ? "on the asset" : `${Math.round(serious.distanceM)} m away`;
    reasons.push(`${serious.change.severity} severity change ${where}`);
    points += 2;
  }
  if (s.confirmed > 0) {
    reasons.push(`${s.confirmed} change(s) confirmed by an analyst`);
    points += 1;
  }
  if (s.nearestM === 0) {
    reasons.push("a change overlaps the asset itself");
    points += 1;
  }
  if (daysSinceLook === null) {
    reasons.push("no usable image of the site in this window");
    points += 1;
  } else if (daysSinceLook > STALE_DAYS) {
    reasons.push(`last usable image ${daysSinceLook} days old`);
    points += 1;
  }
  if (valueRatio !== undefined && valueRatio >= 2 && points > 0) {
    reasons.push(`value ${valueRatio.toFixed(1)}× the portfolio median`);
    points += 1;
  }

  const action: Action = points >= 3 ? "Inspect" : points >= 1 ? "Desk review" : "No action";
  if (action === "No action") reasons.push("no change, hazard or staleness flag");
  return { action, reasons, priority: points * 10 + s.score / 10 };
}

export const ACTION_TONE: Record<Action, string> = {
  Inspect: "border-crit/50 bg-crit/10 text-crit",
  "Desk review": "border-med/50 bg-med/10 text-med",
  "No action": "border-accent/40 text-accent",
  "Not observed": "border-info/50 text-info",
};
