"""What can actually be seen, at what resolution, from what sensor.

This module exists because the gap between what a customer asks for and what
physics permits is where geospatial products lose their credibility. A system
that accepts "detect people and drones" and then returns zero results has told
the analyst something false by implication: that it looked, and found none.

The rule of thumb across the remote-sensing literature is the Johnson criteria
and its modern descendants: *detection* (something is there) needs roughly 2-3
pixels across the object's smallest dimension, *classification* (what kind of
thing) around 6-8, and *identification* (which specific one) 12 or more. This
module applies that arithmetic honestly, including where it produces an answer
nobody wants to hear.

Two requests come up often enough to name directly:

**People.** A standing adult presents roughly 0.5 m. At Sentinel-2's 10 m that
is one four-hundredth of a pixel by area. At the sharpest commercial optical
available -- about 30 cm -- a person spans one to two pixels, which is below
detection and far below anything that could be called tracking. Satellite
imagery does not see people, and a vendor who says otherwise is selling
something else. Crowds are sometimes inferable as texture; individuals are not.
UAV imagery at 2-5 cm is a different matter entirely, which is why the UAV
path exists in `Platform`.

**Drones in flight.** A small UAV is around 0.3 m and moving. It is sub-pixel
at every satellite resolution, and a satellite's integration time smears a
moving sub-pixel object into nothing. Airborne drone detection is an RF,
acoustic or radar problem and is not solvable with Earth observation at any
price. What imagery *can* do is find the ground signature: launch and recovery
sites, ground control stations, catapults, and the apron activity around them.
That distinction is the difference between a capability and a claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .domain import ObjectClass, Sensor


class Task(str, Enum):
    """What is being asked of the imagery, in increasing difficulty."""

    DETECT = "detect"              # something is there
    CLASSIFY = "classify"          # what kind of thing it is
    IDENTIFY = "identify"          # which specific one
    TRACK = "track"                # follow it between acquisitions


#: Pixels across the object's *smallest* dimension needed for each task.
#: These follow the Johnson criteria as used in imagery-intelligence practice.
#: They are deliberately on the demanding side: a detector that needs three
#: pixels and is given two returns noise, and noise at a border sector is worse
#: than an honest gap.
PIXELS_REQUIRED: dict[Task, float] = {
    Task.DETECT: 2.5,
    Task.CLASSIFY: 6.0,
    Task.IDENTIFY: 12.0,
    Task.TRACK: 6.0,
}


class Platform(str, Enum):
    """Where the pixels come from, with the resolution each actually delivers."""

    SENTINEL_2 = "sentinel-2"            # 10 m
    SENTINEL_1 = "sentinel-1"            # 10 m SAR
    LANDSAT = "landsat"                  # 30 m
    PLANETSCOPE = "planetscope"          # 3 m
    SKYSAT = "skysat"                    # 0.5 m
    WORLDVIEW = "worldview"              # 0.31 m
    UAV = "uav"                          # 0.02-0.05 m


GSD_M: dict[Platform, float] = {
    Platform.SENTINEL_2: 10.0,
    Platform.SENTINEL_1: 10.0,
    Platform.LANDSAT: 30.0,
    Platform.PLANETSCOPE: 3.0,
    Platform.SKYSAT: 0.5,
    Platform.WORLDVIEW: 0.31,
    Platform.UAV: 0.03,
}


@dataclass(frozen=True)
class Target:
    """A thing someone wants found, and its real dimensions in metres."""

    name: str
    length_m: float
    width_m: float
    object_class: ObjectClass | None = None
    moving: bool = False
    #: Set when no Earth-observation platform can do this at all, with the
    #: reason. Present so the answer is a sentence rather than an empty list.
    impossible_from_orbit: str = ""
    sar_visible: bool = True

    @property
    def smallest_m(self) -> float:
        return min(self.length_m, self.width_m)


#: The catalogue of what customers ask for, including the ones that cannot be
#: delivered. Dimensions are real: a shipping container is 2.44 m wide, a
#: Toyota Hilux is 1.9 m, an F-16 has a 9.8 m span, a person is about 0.5 m.
TARGETS: dict[str, Target] = {
    t.name: t for t in (
        # -- ground vehicles ------------------------------------------------
        Target("car", 4.5, 1.8, ObjectClass.VEHICLE, moving=True),
        Target("truck", 12.0, 2.5, ObjectClass.TRUCK, moving=True),
        Target("construction_vehicle", 8.0, 3.0,
               ObjectClass.CONSTRUCTION_VEHICLE, moving=True),
        Target("container_stack", 12.2, 2.44, ObjectClass.CONTAINER_STACK),

        # -- aircraft -------------------------------------------------------
        Target("fighter_aircraft", 15.0, 9.8, ObjectClass.AIRCRAFT),
        Target("transport_aircraft", 45.0, 45.0, ObjectClass.AIRCRAFT),
        Target("helicopter", 17.0, 3.0, ObjectClass.HELICOPTER),

        # -- maritime -------------------------------------------------------
        Target("container_ship", 300.0, 45.0, ObjectClass.VESSEL),
        Target("patrol_boat", 30.0, 6.0, ObjectClass.VESSEL),
        Target("small_boat", 8.0, 2.5, ObjectClass.SMALL_BOAT),

        # -- structures and construction -------------------------------------
        Target("building", 20.0, 15.0, ObjectClass.BUILDING),
        Target("large_structure", 100.0, 60.0, ObjectClass.BUILDING),
        Target("storage_tank", 30.0, 30.0, ObjectClass.STORAGE_TANK),
        Target("runway", 3000.0, 45.0, ObjectClass.RUNWAY),
        Target("road_or_track", 500.0, 6.0, ObjectClass.ROAD),
        Target("bridge", 200.0, 12.0, ObjectClass.BRIDGE),
        Target("solar_array", 200.0, 100.0, ObjectClass.SOLAR_ARRAY),

        # -- the ones that cannot be done from orbit -------------------------
        Target(
            "person", 0.5, 0.4, None, moving=True,
            impossible_from_orbit=(
                "a standing adult is about 0.5 m. At 10 m that is one "
                "four-hundredth of a pixel by area, and even at the sharpest "
                "commercial optical available -- about 30 cm -- a person spans "
                "one to two pixels, which is below detection and far below "
                "anything that could be called tracking. Dense crowds are "
                "sometimes inferable as a texture change over a known surface; "
                "individual people are not visible from orbit at any price. "
                "UAV imagery at 2-5 cm can resolve people, and brings privacy "
                "and legal obligations that satellite monitoring does not."),
            sar_visible=False),
        Target(
            "drone_in_flight", 0.3, 0.3, None, moving=True,
            impossible_from_orbit=(
                "a small UAV is around 0.3 m and moving. It is sub-pixel at "
                "every satellite resolution, and a satellite's integration "
                "time smears a moving sub-pixel object into the background. "
                "Airborne drone detection is an RF, acoustic or radar problem "
                "and is not solvable with Earth observation at any price. What "
                "imagery does find is the ground signature: launch and "
                "recovery sites, ground control stations and the apron "
                "activity around them -- see the 'drone_ground_station' "
                "target."),
            sar_visible=False),
        Target("drone_ground_station", 12.0, 6.0, ObjectClass.BUILDING),
    )
}


@dataclass(frozen=True)
class Verdict:
    """Whether a platform can do a task on a target, and why."""

    target: str
    task: Task
    platform: Platform
    feasible: bool
    pixels_across: float
    pixels_needed: float
    reason: str

    def to_dict(self) -> dict:
        return {"target": self.target, "task": self.task.value,
                "platform": self.platform.value, "feasible": self.feasible,
                "pixels_across": round(self.pixels_across, 2),
                "pixels_needed": self.pixels_needed, "reason": self.reason}


def assess(target_name: str, task: Task, platform: Platform) -> Verdict:
    """Can this platform do this task on this target? With the arithmetic."""
    target = TARGETS.get(target_name)
    if target is None:
        known = ", ".join(sorted(TARGETS))
        raise KeyError(f"unknown target {target_name!r}; known: {known}")

    gsd = GSD_M[platform]
    across = target.smallest_m / gsd
    needed = PIXELS_REQUIRED[task]

    if target.impossible_from_orbit and platform is not Platform.UAV:
        return Verdict(target_name, task, platform, False, across, needed,
                       target.impossible_from_orbit)

    if platform is Platform.SENTINEL_1 and not target.sar_visible:
        return Verdict(target_name, task, platform, False, across, needed,
                       "SAR returns a backscatter signature, and this target "
                       "does not present one distinguishable from its "
                       "surroundings")

    if target.moving and task is Task.TRACK and platform is not Platform.UAV:
        #: Revisit, not resolution, is the binding constraint. Sentinel-2
        #: passes every five days; a car has gone somewhere else.
        return Verdict(
            target_name, task, platform, False, across, needed,
            "tracking a moving object needs revisit faster than the object "
            "moves. Satellite revisit is measured in days and a vehicle moves "
            "in seconds, so what imagery supports is counting a population at "
            "each pass -- how many vehicles are present -- not following one "
            "between passes")

    feasible = across >= needed
    if feasible:
        reason = (f"{target.smallest_m:.2f} m across {gsd:.2f} m pixels is "
                  f"{across:.1f} pixels, at or above the {needed:.1f} needed "
                  f"to {task.value}")
    else:
        reason = (f"{target.smallest_m:.2f} m across {gsd:.2f} m pixels is "
                  f"{across:.1f} pixels, below the {needed:.1f} needed to "
                  f"{task.value}. The information is not in the data, so no "
                  f"model recovers it")
    return Verdict(target_name, task, platform, feasible, across, needed, reason)


def platforms_for(target_name: str, task: Task = Task.DETECT) -> list[Platform]:
    """Every platform that can do this task on this target, cheapest first."""
    return [p for p in sorted(GSD_M, key=lambda x: -GSD_M[x])
            if assess(target_name, task, p).feasible]


def cheapest_platform(target_name: str,
                      task: Task = Task.DETECT) -> Platform | None:
    """The coarsest -- and so usually cheapest -- platform that still works.

    Coarser is cheaper: Sentinel is free and WorldView is not. Recommending the
    sharpest sensor that works is how an imagery bill becomes a surprise.
    """
    options = platforms_for(target_name, task)
    return options[0] if options else None


def capability_matrix(task: Task = Task.DETECT) -> list[dict]:
    """Every target against every platform. What the sales conversation needs."""
    rows = []
    for name in sorted(TARGETS):
        target = TARGETS[name]
        row = {"target": name,
               "smallest_m": target.smallest_m,
               "object_class": target.object_class.value
               if target.object_class else None,
               "possible_from_orbit": not target.impossible_from_orbit}
        for platform in GSD_M:
            row[platform.value] = assess(name, task, platform).feasible
        row["cheapest"] = (c.value if (c := cheapest_platform(name, task))
                           else None)
        rows.append(row)
    return rows


def explain(target_name: str, task: Task = Task.DETECT) -> str:
    """A sentence an analyst or a buyer can act on."""
    target = TARGETS.get(target_name)
    if target is None:
        known = ", ".join(sorted(TARGETS))
        raise KeyError(f"unknown target {target_name!r}; known: {known}")

    if target.impossible_from_orbit:
        uav = assess(target_name, task, Platform.UAV)
        tail = (f" UAV imagery at {GSD_M[Platform.UAV] * 100:.0f} cm can: "
                f"{uav.reason}." if uav.feasible else "")
        return (f"{target_name}: not possible from orbit. "
                f"{target.impossible_from_orbit}{tail}")

    best = cheapest_platform(target_name, task)
    if best is None:
        return (f"{target_name}: no available platform can {task.value} this "
                f"target -- it is {target.smallest_m:.2f} m across and the "
                f"sharpest platform configured is "
                f"{min(GSD_M.values()) * 100:.0f} cm")
    verdict = assess(target_name, task, best)
    return (f"{target_name}: {task.value} needs {verdict.pixels_needed:.1f} "
            f"pixels across {target.smallest_m:.2f} m. Cheapest platform that "
            f"works is {best.value} at {GSD_M[best]:.2f} m "
            f"({verdict.pixels_across:.1f} pixels).")
