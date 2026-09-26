"""Model governance: which version produced a finding, and how it was doing.

Every finding already carries a `model_version` string. That is enough to
answer "which model said this" and not enough for the question a customer
actually asks in year two: *this alert was wrong -- what else did that version
produce, and when did we start using it?*

Answering that needs three things this module provides:

**A registry, so a version is a record rather than a string.** When it was
registered, what it was measured at, whether it is active, and who activated
it. A version that appears in findings but not in the registry is a
deployment nobody recorded, which the health check reports rather than hides.

**An inference log, so a version's outputs are enumerable.** Not the outputs
themselves -- those are the findings -- but the fact of each run, its inputs
and the decision. Withdrawing a bad version then means listing exactly what it
touched instead of guessing from dates.

**Measured performance attached to the version, not the product.** A precision
figure is only meaningful beside the version and the test set that produced it.
`record_evaluation` keeps them together so that "precision 0.85" can never be
quoted without both.

One rule enforced here rather than documented: a version cannot be activated
without a recorded evaluation. Promoting an unmeasured model is how a detector
silently gets worse, and the failure is invisible precisely because nobody
measured it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Stage(str, Enum):
    REGISTERED = "registered"    # known, never run in anger
    ACTIVE = "active"            # producing findings now
    RETIRED = "retired"          # superseded, findings remain valid
    WITHDRAWN = "withdrawn"      # found to be wrong; its findings are suspect


#: A version may not be activated below this F1 on its own test set. Not a
#: quality bar -- it is a floor that catches a model wired up wrong, which
#: scores near zero rather than merely badly.
MIN_ACTIVATION_F1 = 0.30


class RegistryError(Exception):
    """An operation that would leave the record unable to answer its question."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Evaluation:
    """What a version scored, and on what. The two are inseparable."""

    version: str
    test_set: str
    at: datetime
    precision: float
    recall: float
    samples: int
    notes: str = ""

    @property
    def f1(self) -> float:
        total = self.precision + self.recall
        return 0.0 if total <= 0 else 2 * self.precision * self.recall / total

    def to_dict(self) -> dict:
        return {"version": self.version, "test_set": self.test_set,
                "at": self.at.isoformat(), "precision": round(self.precision, 4),
                "recall": round(self.recall, 4), "f1": round(self.f1, 4),
                "samples": self.samples, "notes": self.notes}


@dataclass
class ModelVersion:
    """One version of one model, and everything recorded about it."""

    name: str
    version: str
    stage: Stage = Stage.REGISTERED
    registered_at: datetime = field(default_factory=_now)
    registered_by: str = ""
    activated_at: datetime | None = None
    activated_by: str = ""
    withdrawn_reason: str = ""
    kind: str = "change"            # change | detection | baseline
    evaluations: list[Evaluation] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.name}:{self.version}"

    @property
    def best(self) -> Evaluation | None:
        return max(self.evaluations, key=lambda e: e.f1, default=None)

    @property
    def latest(self) -> Evaluation | None:
        return max(self.evaluations, key=lambda e: e.at, default=None)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "stage": self.stage.value, "kind": self.kind,
            "registered_at": self.registered_at.isoformat(),
            "registered_by": self.registered_by,
            "activated_at": (self.activated_at.isoformat()
                             if self.activated_at else None),
            "activated_by": self.activated_by,
            "withdrawn_reason": self.withdrawn_reason,
            "evaluations": [e.to_dict() for e in self.evaluations],
            "latest_evaluation": (self.latest.to_dict() if self.latest
                                  else None),
        }


@dataclass(frozen=True)
class Inference:
    """The fact that a version ran, with what, and what it decided.

    The finding itself is not duplicated here -- it lives in the store and is
    referenced. What this adds is enumerability: given a version, list
    everything it touched.
    """

    version_id: str
    at: datetime
    aoi_id: str
    inputs: str            # a digest of the scene ids, not the pixels
    finding_id: str = ""
    outcome: str = ""      # what it decided, in one word
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {"version_id": self.version_id, "at": self.at.isoformat(),
                "aoi_id": self.aoi_id, "inputs": self.inputs,
                "finding_id": self.finding_id, "outcome": self.outcome,
                "confidence": round(self.confidence, 3)}


def digest_inputs(*parts: str) -> str:
    """A stable short digest of what went in.

    Scene identifiers rather than pixels: the provider can reproduce any scene
    from its id, so the digest is enough to rerun the inference exactly without
    the registry holding imagery it has no business holding.
    """
    seed = "|".join(sorted(str(p) for p in parts if p))
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


class Registry:
    """Model versions, their measurements, and what they produced."""

    def __init__(self) -> None:
        self.versions: dict[str, ModelVersion] = {}
        self.inferences: list[Inference] = []

    # -- lifecycle ---------------------------------------------------------

    def register(self, name: str, version: str, kind: str = "change",
                 by: str = "") -> ModelVersion:
        model = ModelVersion(name=name, version=version, kind=kind,
                             registered_by=by)
        if model.id in self.versions:
            return self.versions[model.id]
        self.versions[model.id] = model
        return model

    def record_evaluation(self, name: str, version: str, test_set: str,
                          precision: float, recall: float, samples: int,
                          notes: str = "") -> Evaluation:
        """Attach a measurement to a version.

        The test set is required, not optional. A precision figure without the
        set it was measured on is a number that cannot be reproduced or
        compared, which is the same as not having one.
        """
        model = self._require(name, version)
        if not test_set.strip():
            raise RegistryError(
                "an evaluation needs the test set it was measured on; without "
                "it the number cannot be reproduced or compared")
        if samples <= 0:
            raise RegistryError("an evaluation needs at least one sample")
        evaluation = Evaluation(model.id, test_set.strip(), _now(),
                                precision, recall, samples, notes)
        model.evaluations.append(evaluation)
        return evaluation

    def activate(self, name: str, version: str, by: str = "") -> ModelVersion:
        """Promote a version to producing findings.

        Refuses an unmeasured version. Promoting one is how a detector silently
        gets worse, and the failure is invisible precisely because nobody
        measured it.
        """
        model = self._require(name, version)
        if model.stage is Stage.WITHDRAWN:
            raise RegistryError(
                f"{model.id} was withdrawn ({model.withdrawn_reason}); "
                "register a new version rather than reactivating this one")
        best = model.best
        if best is None:
            raise RegistryError(
                f"{model.id} has no recorded evaluation. Record one with "
                "record_evaluation before activating it -- an unmeasured "
                "model cannot be known to be better than what it replaces")
        if best.f1 < MIN_ACTIVATION_F1:
            raise RegistryError(
                f"{model.id} scores F1 {best.f1:.2f} on {best.test_set}, below "
                f"{MIN_ACTIVATION_F1:.2f}. That is the range of a model wired "
                "up wrong rather than one that is merely weak")

        for other in self.versions.values():
            if other.name == model.name and other.stage is Stage.ACTIVE:
                other.stage = Stage.RETIRED
        model.stage = Stage.ACTIVE
        model.activated_at = _now()
        model.activated_by = by
        return model

    def withdraw(self, name: str, version: str, reason: str) -> ModelVersion:
        """Mark a version wrong. Its findings become suspect, not deleted.

        Deleting them would destroy the record of what was acted on, which is
        the opposite of what an audit needs after a bad model is found.
        """
        model = self._require(name, version)
        if not reason.strip():
            raise RegistryError("withdrawing a version needs a reason; it is "
                                "what the next person reads")
        model.stage = Stage.WITHDRAWN
        model.withdrawn_reason = reason.strip()
        return model

    def active(self, name: str) -> ModelVersion | None:
        for model in self.versions.values():
            if model.name == name and model.stage is Stage.ACTIVE:
                return model
        return None

    # -- inference ---------------------------------------------------------

    def record_inference(self, name: str, version: str, aoi_id: str,
                         inputs: str, finding_id: str = "",
                         outcome: str = "", confidence: float = 0.0) -> Inference:
        model = self._require(name, version)
        entry = Inference(model.id, _now(), aoi_id, inputs, finding_id,
                          outcome, confidence)
        self.inferences.append(entry)
        return entry

    def produced_by(self, name: str, version: str) -> list[Inference]:
        """Everything a version touched. What a withdrawal needs."""
        target = f"{name}:{version}"
        return [i for i in self.inferences if i.version_id == target]

    def affected_findings(self, name: str, version: str) -> list[str]:
        return [i.finding_id for i in self.produced_by(name, version)
                if i.finding_id]

    # -- health ------------------------------------------------------------

    def health(self, seen_versions: list[str] = ()) -> dict:
        """What an administrator should be shown, with gaps foregrounded.

        `seen_versions` is the set of model strings appearing on stored
        findings. A version in findings but not in the registry is a
        deployment nobody recorded, and it is reported rather than hidden --
        that is exactly the case where provenance has already failed.
        """
        known = set(self.versions)
        unregistered = sorted(set(seen_versions) - known - {""})
        unmeasured = sorted(m.id for m in self.versions.values()
                            if not m.evaluations)
        withdrawn_in_use = sorted(
            m.id for m in self.versions.values()
            if m.stage is Stage.WITHDRAWN and m.id in set(seen_versions))
        return {
            "versions": len(self.versions),
            "active": sorted(m.id for m in self.versions.values()
                             if m.stage is Stage.ACTIVE),
            "inferences": len(self.inferences),
            "unregistered_in_use": unregistered,
            "unmeasured": unmeasured,
            "withdrawn_but_present_in_findings": withdrawn_in_use,
            "healthy": not (unregistered or withdrawn_in_use),
        }

    def _require(self, name: str, version: str) -> ModelVersion:
        model = self.versions.get(f"{name}:{version}")
        if model is None:
            raise RegistryError(
                f"{name}:{version} is not registered; register it before "
                "recording anything about it")
        return model
