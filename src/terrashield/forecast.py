"""Projection of an observed trend, with the uncertainty attached.

This module crosses a line the rest of the product deliberately holds. PRD
section 55 and `copilot.REFUSALS` state that TerraShield describes trends as
trends and does not forecast; that refusal exists because a probabilistic
inference presented as a fact is how an analysis product becomes a liability.
Forecasting was added on an explicit instruction, and this file is written so
that the original concern survives the decision.

Three properties enforced in code rather than asked for in a style guide:

**A projection is never separable from its interval.** `Projection` has no
point-estimate field that can be read on its own. The value is `band`, and the
narrowest question it will answer is "what range, with what confidence".

**A projection is refused when the history cannot support one.** Too few
observations, too much scatter, or a horizon far beyond the observed record all
return a refusal naming the reason, not a number with a wide interval. A wide
interval invites a reader to use its midpoint; a refusal does not.

**The language is conditional and the caveat travels with the payload.**
`headline()` says "if the observed trend continues", every serialised form
carries `is_projection: True` and the assumption text, and a test asserts the
words *will*, *predicts* and *forecast to be* never appear in generated text.

What this is: least-squares extrapolation of a measured series with a
prediction interval widened for distance beyond the data. What it is not: a
model of anything causal. It knows nothing about why a number moved, so it
cannot know whether the reason will persist -- which is the assumption the
whole output rests on and the reason it is stated in every line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from .baseline import Observation

#: Fewer points than this and a straight line through them means nothing. Six
#: is two Sentinel-2 revisit cycles at these latitudes -- enough that weather
#: has not decided the answer on its own.
MIN_OBSERVATIONS = 6

#: A projection may reach this far past the end of the record, as a fraction of
#: the record's own length. Beyond it the honest answer is that the data does
#: not reach. Half is conservative and deliberately so: the failure mode of
#: extrapolation is confident nonsense far from the data.
MAX_HORIZON_FRACTION = 0.5

#: Below this, the "trend" is scatter. R-squared of 0.3 is a weak fit, and
#: weaker than that is a line drawn through noise.
MIN_FIT_QUALITY = 0.30

#: Multiples of the residual standard error for each confidence level. Normal
#: quantiles: a projection is not more precise than the scatter it came from.
Z_FOR: dict[int, float] = {50: 0.674, 80: 1.282, 90: 1.645, 95: 1.960}
DEFAULT_CONFIDENCE = 80


class ProjectionRefused(Exception):
    """The history cannot support a projection, with the reason."""


@dataclass(frozen=True)
class Band:
    """A range. There is deliberately no bare point estimate beside it."""

    low: float
    high: float
    confidence: int

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def midpoint(self) -> float:
        """The centre of the interval.

        Exists because plotting needs a line to draw. It is *not* a prediction
        and is not exposed in any serialised form -- reading it alone discards
        exactly the information the interval was computed to carry.
        """
        return (self.low + self.high) / 2.0

    def to_dict(self) -> dict:
        return {"low": round(self.low, 3), "high": round(self.high, 3),
                "confidence_pct": self.confidence}


@dataclass(frozen=True)
class Projection:
    """Where a measured trend reaches, if it continues, with what spread."""

    aoi_id: str
    metric: str
    band: Band
    as_of: date
    horizon: date
    slope_per_day: float
    fit_quality: float
    observations: int
    observed_from: date
    observed_to: date
    assumption: str = (
        "this is an extrapolation of the observed trend, not a model of "
        "cause. It assumes whatever drove the measured change continues "
        "unchanged, which the system has no way to verify")

    @property
    def days_ahead(self) -> int:
        return (self.horizon - self.observed_to).days

    def headline(self) -> str:
        """Conditional by construction. Never asserts that something will happen."""
        direction = ("rising" if self.slope_per_day > 0 else
                     "falling" if self.slope_per_day < 0 else "flat")
        return (
            f"if the observed trend continues, {self.metric} at {self.aoi_id} "
            f"would fall between {self.band.low:.1f} and {self.band.high:.1f} "
            f"by {self.horizon.isoformat()} ({self.band.confidence}% interval). "
            f"The series is {direction} over {self.observations} observations "
            f"from {self.observed_from.isoformat()} to "
            f"{self.observed_to.isoformat()}; fit quality "
            f"{self.fit_quality:.2f}.")

    def to_dict(self) -> dict:
        return {
            "aoi_id": self.aoi_id,
            "metric": self.metric,
            "is_projection": True,
            "band": self.band.to_dict(),
            "horizon": self.horizon.isoformat(),
            "days_ahead": self.days_ahead,
            "slope_per_day": round(self.slope_per_day, 5),
            "fit_quality": round(self.fit_quality, 3),
            "observations": self.observations,
            "observed_from": self.observed_from.isoformat(),
            "observed_to": self.observed_to.isoformat(),
            "assumption": self.assumption,
            "headline": self.headline(),
        }


def project(aoi_id: str, metric: str, history: list[Observation],
            horizon: date, confidence: int = DEFAULT_CONFIDENCE) -> Projection:
    """Project a measured series forward, or refuse and say why.

    Refuses rather than returning a very wide band, because a wide band invites
    a reader to take its midpoint, and the midpoint of a meaningless interval is
    a meaningless number that looks like an answer.
    """
    if confidence not in Z_FOR:
        raise ValueError(f"confidence must be one of {sorted(Z_FOR)}")

    points = sorted((o for o in history if o.metric == metric),
                    key=lambda o: o.when)
    if len(points) < MIN_OBSERVATIONS:
        raise ProjectionRefused(
            f"{metric} at {aoi_id} has {len(points)} observation(s); at least "
            f"{MIN_OBSERVATIONS} are needed before a trend line means anything")

    first, last = points[0].when, points[-1].when
    span_days = (last - first).days
    if span_days <= 0:
        raise ProjectionRefused(
            f"{metric} at {aoi_id} spans no time; a rate needs a duration")

    ahead = (horizon - last).days
    if ahead <= 0:
        raise ProjectionRefused(
            f"the horizon {horizon.isoformat()} is not after the last "
            f"observation {last.isoformat()}; that is a lookup, not a projection")

    reach = span_days * MAX_HORIZON_FRACTION
    if ahead > reach:
        raise ProjectionRefused(
            f"{horizon.isoformat()} is {ahead} days past the record, which "
            f"covers {span_days} days. Projections are capped at "
            f"{MAX_HORIZON_FRACTION:.0%} of the observed span "
            f"({reach:.0f} days) -- beyond that the honest answer is that the "
            f"data does not reach")

    xs = [float((o.when - first).days) for o in points]
    ys = [float(o.value) for o in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        raise ProjectionRefused(
            f"{metric} at {aoi_id} has no spread in time to fit against")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in residuals)
    #: A perfectly flat series fits perfectly; calling that a bad fit would
    #: refuse the one case where the projection is most trustworthy.
    fit = 1.0 if ss_tot <= 1e-12 else max(0.0, 1.0 - ss_res / ss_tot)
    if fit < MIN_FIT_QUALITY:
        raise ProjectionRefused(
            f"{metric} at {aoi_id} fits a straight line at R2 {fit:.2f}, below "
            f"{MIN_FIT_QUALITY:.2f}. The series is scatter rather than a "
            f"trend, and a line through it would project noise")

    x_future = float((horizon - first).days)
    centre = intercept + slope * x_future

    #: Residual standard error, widened by the standard prediction-interval
    #: term. The (x - mean)^2 / Sxx part is why the band opens out the further
    #: the horizon sits from the middle of the data -- which is the entire
    #: reason this is not just a line.
    dof = max(1, n - 2)
    stderr = math.sqrt(ss_res / dof) if ss_res > 0 else 0.0
    leverage = 1.0 + (1.0 / n) + ((x_future - mean_x) ** 2) / sxx
    spread = Z_FOR[confidence] * stderr * math.sqrt(leverage)

    return Projection(
        aoi_id=aoi_id, metric=metric,
        band=Band(low=centre - spread, high=centre + spread,
                  confidence=confidence),
        as_of=last, horizon=horizon, slope_per_day=slope, fit_quality=fit,
        observations=n, observed_from=first, observed_to=last)


def try_project(aoi_id: str, metric: str, history: list[Observation],
                horizon: date,
                confidence: int = DEFAULT_CONFIDENCE) -> Projection | dict:
    """`project`, returning the refusal as data instead of raising.

    For the API and the console, where "we cannot project this, because ..." is
    a legitimate answer to render and not an error.
    """
    try:
        return project(aoi_id, metric, history, horizon, confidence)
    except ProjectionRefused as e:
        return {"aoi_id": aoi_id, "metric": metric, "is_projection": False,
                "refused": str(e)}


def horizon_limit(history: list[Observation], metric: str) -> date | None:
    """The furthest date a projection of this series would be allowed to reach."""
    points = sorted((o for o in history if o.metric == metric),
                    key=lambda o: o.when)
    if len(points) < MIN_OBSERVATIONS:
        return None
    span = (points[-1].when - points[0].when).days
    if span <= 0:
        return None
    return points[-1].when + timedelta(days=int(span * MAX_HORIZON_FRACTION))
