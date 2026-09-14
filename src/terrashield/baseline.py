"""Pattern of life: what normal looks like at one site, learned from that site.

PRD section 16 calls this the long-term differentiator and that is right, but
for an unobvious reason. The moat is not the algorithm -- it is a dozen lines
of robust statistics. The moat is that a baseline takes months of a specific
AOI's history to build, so the second vendor to monitor a customer's port
starts a year behind the first, however good their models are.

Three decisions do most of the work.

**Median and MAD, not mean and standard deviation.** One cloudy scene or one
genuinely unusual day would otherwise poison the definition of normal for
weeks, and a baseline that moves toward the anomaly is a baseline that stops
detecting it. That is the classic failure of naive pattern-of-life: monitor a
site through a slow build-up and the system silently accepts it as the new
normal.

**A weekday term.** A container port handles three times as many trucks on
Tuesday as on Sunday. A baseline without a weekday term reports an anomaly
every weekend, forever, and the analyst turns the alerts off.

**Baselines are tied to an AOI's geometry fingerprint.** Redraw the boundary
and the history does not carry over, because it was measured over a different
patch of ground. Silently carrying it is how a platform ends up defending a
number it cannot explain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .domain import BaselineStat
from .raster import percentile

#: How much history a baseline wants. Long enough to see a site's weekly and
#: monthly rhythm; short enough that a genuine step change is eventually
#: absorbed rather than flagged forever.
DEFAULT_WINDOW_DAYS = 120

#: Below this, the phrase "compared with its historical baseline" is a lie.
#: Findings are still produced, with the sample size attached and confidence
#: scaled down, because a new site has to be monitored from day one -- but the
#: analyst is told what the comparison rests on.
MIN_SAMPLES = 6

#: Per-weekday medians need enough of each weekday to mean anything.
MIN_SAMPLES_FOR_WEEKDAY = 21


@dataclass(frozen=True)
class Observation:
    """One measurement of one metric at one AOI, on one date.

    `quality` is the fraction of the AOI that was actually visible. A vehicle
    count taken through a hole in the cloud is not comparable with one taken on
    a clear day, and weighting it equally is how a cloudy Tuesday becomes an
    anomaly.
    """

    aoi_id: str
    metric: str
    when: date
    value: float
    quality: float = 1.0
    scene_id: str = ""

    @property
    def usable(self) -> bool:
        return self.quality >= 0.6


@dataclass
class Baseline:
    """Normal for one metric at one AOI, with and without a weekday term."""

    aoi_id: str
    metric: str
    aoi_fingerprint: str
    overall: BaselineStat
    weekday: dict[int, BaselineStat] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return self.overall.n

    @property
    def has_weekday_term(self) -> bool:
        return bool(self.weekday)

    def stat_for(self, when: date) -> BaselineStat:
        """The most specific baseline that has enough support for this date."""
        wd = self.weekday.get(when.weekday())
        if wd is not None and wd.n >= 3:
            return wd
        return self.overall

    def deviation(self, when: date, value: float) -> float:
        return self.stat_for(when).z(value)

    def describe(self, when: date, value: float) -> str:
        stat = self.stat_for(when)
        z = stat.z(value)
        scope = ("for a " + when.strftime("%A") if stat is not self.overall
                 else "across all days")
        direction = "above" if z >= 0 else "below"
        return (f"{self.metric.replace('_', ' ')} is {value:,.0f} against a "
                f"median of {stat.median:,.0f} {scope} "
                f"(n={stat.n}, {abs(z):.1f} robust deviations {direction})")

    def to_dict(self) -> dict:
        return {
            "aoi_id": self.aoi_id,
            "metric": self.metric,
            "aoi_fingerprint": self.aoi_fingerprint,
            "n": self.overall.n,
            "median": round(self.overall.median, 3),
            "mad": round(self.overall.mad, 4),
            "window_start": self.overall.window_start.isoformat(),
            "window_end": self.overall.window_end.isoformat(),
            "weekday_medians": {
                str(k): round(v.median, 2) for k, v in sorted(self.weekday.items())
            },
        }


def _stat(aoi_id: str, metric: str, rows: list[Observation],
          start: date, end: date) -> BaselineStat:
    values = [o.value for o in rows]
    med = percentile(values, 50.0)
    mad = percentile([abs(v - med) for v in values], 50.0)
    return BaselineStat(aoi_id=aoi_id, metric=metric, median=med, mad=mad,
                        n=len(values), window_start=start, window_end=end)


def build(observations: list[Observation], metric: str, as_of: date,
          aoi_fingerprint: str = "",
          window_days: int = DEFAULT_WINDOW_DAYS) -> Baseline | None:
    """Build a baseline for one metric from history strictly before `as_of`.

    Strictly before, because a baseline that includes the day under assessment
    is comparing a value with itself. On a single dramatic day that dilution is
    small; on a short history it is enough to hide the event entirely.
    """
    start = as_of - timedelta(days=window_days)
    rows = [o for o in observations
            if o.metric == metric and o.usable and start <= o.when < as_of]
    if not rows:
        return None
    rows.sort(key=lambda o: o.when)
    aoi_id = rows[0].aoi_id
    overall = _stat(aoi_id, metric, rows, rows[0].when, rows[-1].when)

    weekday: dict[int, BaselineStat] = {}
    if len(rows) >= MIN_SAMPLES_FOR_WEEKDAY:
        buckets: dict[int, list[Observation]] = {}
        for o in rows:
            buckets.setdefault(o.when.weekday(), []).append(o)
        for wd, group in buckets.items():
            if len(group) >= 3:
                weekday[wd] = _stat(aoi_id, metric, group,
                                    group[0].when, group[-1].when)

    return Baseline(aoi_id=aoi_id, metric=metric,
                    aoi_fingerprint=aoi_fingerprint,
                    overall=overall, weekday=weekday)


def build_all(observations: list[Observation], as_of: date,
              aoi_fingerprint: str = "",
              window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Baseline]:
    metrics = sorted({o.metric for o in observations})
    out: dict[str, Baseline] = {}
    for m in metrics:
        b = build(observations, m, as_of, aoi_fingerprint, window_days)
        if b is not None:
            out[m] = b
    return out


#: How far back a trend is fitted. Long enough for a gradual build-up to show
#: a slope, short enough that last season is not evidence about this one.
TREND_WINDOW_DAYS = 60


def trend(observations: list[Observation], metric: str, as_of: date,
          window_days: int = TREND_WINDOW_DAYS) -> float:
    """Slope of the metric over the window, in units per day.

    Least squares over time. A slow build-up is invisible to any single-day
    deviation test precisely because each day is close to the last -- the
    baseline creeps along with it. The slope is what catches it, and it is why
    the anomaly engine takes both.
    """
    start = as_of - timedelta(days=window_days)
    rows = sorted((o for o in observations
                   if o.metric == metric and o.usable and start <= o.when <= as_of),
                  key=lambda o: o.when)
    if len(rows) < 4:
        return 0.0
    xs = [(o.when - rows[0].when).days for o in rows]
    ys = [o.value for o in rows]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom < 1e-9:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
