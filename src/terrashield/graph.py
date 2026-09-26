"""The ontology, and the evidence chain as a walkable graph.

Both proposals specify a knowledge graph and both reach for Neo4j. That is the
right call at scale and the wrong first move here: a graph database is another
service to deploy, secure, back up and air-gap, and the query this product
actually needs is not a graph query at all. It is a *path* --

    Alert -> Finding -> Event -> Observation -> Scene -> Source

-- walked backwards from a conclusion to the pixels it came from. That is the
question an analyst asks ("why am I being shown this?") and the one an auditor
asks ("what was this based on?"), and it is a traversal of bounded depth over a
few thousand nodes per area.

So this is an in-memory typed graph built from what the store already holds,
with the ontology as an enum rather than a schema in another system. It answers
`evidence_path`, `neighbours` and `subgraph` in microseconds, it has nothing to
deploy, and when a customer's estate outgrows it the ontology below is the
migration target rather than something to redesign.

What it is not: an inference engine. No edge is created that was not measured
or recorded. `Relation.MATCHES` between a detection and an AIS report means a
correlation was computed and passed its test, not that the system concluded
anything about a vessel.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class NodeKind(str, Enum):
    """The ontology. Every entity this product reasons about, and nothing else.

    Notably absent: Person, Organization and Actor. The schema has nowhere to
    record who did something, which is the same boundary `domain.py` holds --
    adding them would be a reviewable act rather than an oversight.
    """

    ORGANISATION = "organisation"     # the tenant
    AREA = "area"                     # an AOI
    FIELD = "field"                   # a named part of one
    SCENE = "scene"                   # one acquisition
    SOURCE = "source"                 # the provider it came from
    SENSOR = "sensor"                 # what took it
    OBSERVATION = "observation"       # one measurement
    DETECTION = "detection"           # one object found
    CHANGE = "change"                 # one difference between two scenes
    EVENT = "event"                   # findings fused into one episode
    ANOMALY = "anomaly"               # a deviation from baseline
    ALERT = "alert"                   # something raised to an analyst
    EVIDENCE = "evidence"             # the bundle behind a finding
    MODEL = "model"                   # the version that produced it
    VESSEL_REPORT = "vessel_report"   # an AIS position claim
    REVIEW = "review"                 # an analyst's recorded decision


class Relation(str, Enum):
    """Typed edges. Each says what was recorded, never what was inferred."""

    CONTAINS = "contains"             # area contains field
    LOCATED_IN = "located_in"         # finding located in area or field
    OBSERVED_IN = "observed_in"       # finding observed in scene
    DERIVED_FROM = "derived_from"     # change derived from two scenes
    PRODUCED_BY = "produced_by"       # finding produced by model
    DELIVERED_BY = "delivered_by"     # scene delivered by source
    CAPTURED_BY = "captured_by"       # scene captured by sensor
    COMPOSES = "composes"             # finding composes an event
    EVIDENCES = "evidences"           # evidence bundle supports a finding
    RAISED_BY = "raised_by"           # alert raised by a finding
    DEVIATES_FROM = "deviates_from"   # anomaly deviates from a baseline
    MATCHES = "matches"               # detection correlates with a report
    REVIEWED_BY = "reviewed_by"       # finding carries an analyst decision
    PRECEDED_BY = "preceded_by"       # event follows another at one place


#: Walking an evidence path means going *back* towards the pixels, so the
#: traversal follows these in reverse: an alert was raised by a finding, which
#: composes an event, which was observed in a scene, which came from a source.
TOWARDS_SOURCE = (
    Relation.RAISED_BY, Relation.COMPOSES, Relation.EVIDENCES,
    Relation.OBSERVED_IN, Relation.DERIVED_FROM, Relation.DELIVERED_BY,
    Relation.CAPTURED_BY, Relation.PRODUCED_BY, Relation.MATCHES,
    Relation.LOCATED_IN, Relation.DEVIATES_FROM,
)

#: Depth cap. The longest legitimate chain is alert to source, which is five
#: hops; ten leaves room for the graph to grow without letting a cycle run away.
MAX_DEPTH = 10


@dataclass(frozen=True)
class Node:
    id: str
    kind: NodeKind
    label: str = ""
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind.value,
                "label": self.label or self.id, **(
                    {"attrs": self.attrs} if self.attrs else {})}


@dataclass(frozen=True)
class Edge:
    src: str
    rel: Relation
    dst: str

    def to_dict(self) -> dict:
        return {"from": self.src, "rel": self.rel.value, "to": self.dst}


class Graph:
    """A typed directed graph, small enough to hold and fast enough to walk."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.out: dict[str, list[Edge]] = {}
        self.inc: dict[str, list[Edge]] = {}

    # -- building ----------------------------------------------------------

    def add(self, node_id: str, kind: NodeKind, label: str = "",
            attrs: dict | None = None) -> Node:
        """Add or update a node. Idempotent: the same id is the same thing.

        Attributes arrive as a dict rather than keyword arguments on purpose.
        With **kwargs, an attribute called `kind`, `label` or `id` collides
        with the parameter of the same name -- and those are exactly the
        attribute names a geospatial record tends to have.
        """
        attrs = attrs or {}
        if not node_id:
            raise ValueError("a node needs an id")
        node = Node(node_id, kind, label, attrs)
        self.nodes[node_id] = node
        self.out.setdefault(node_id, [])
        self.inc.setdefault(node_id, [])
        return node

    def link(self, src: str, rel: Relation, dst: str) -> Edge | None:
        """Record a relationship. Both ends must already exist.

        Silently ignoring a dangling edge would let the graph claim a path that
        does not resolve, which is worse than a missing edge: an evidence chain
        that ends in a node nobody can open is an evidence chain that fails its
        own audit.
        """
        if src not in self.nodes or dst not in self.nodes:
            return None
        edge = Edge(src, rel, dst)
        if edge in self.out[src]:
            return edge
        self.out[src].append(edge)
        self.inc[dst].append(edge)
        return edge

    # -- reading -----------------------------------------------------------

    def neighbours(self, node_id: str,
                   rels: tuple[Relation, ...] = ()) -> list[tuple[Edge, Node]]:
        """Everything one hop away, in either direction."""
        found = []
        for edge in self.out.get(node_id, []):
            if not rels or edge.rel in rels:
                found.append((edge, self.nodes[edge.dst]))
        for edge in self.inc.get(node_id, []):
            if not rels or edge.rel in rels:
                found.append((edge, self.nodes[edge.src]))
        return found

    def evidence_path(self, node_id: str) -> list[Node]:
        """Walk back from a conclusion to the pixels it rests on.

        Breadth-first along the relations that lead towards a source, so the
        answer to "why am I being shown this?" is a list the analyst can open
        in order.
        """
        if node_id not in self.nodes:
            return []
        seen = {node_id}
        order = [self.nodes[node_id]]
        queue = deque([(node_id, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= MAX_DEPTH:
                continue
            for edge in self.out.get(current, []):
                if edge.rel not in TOWARDS_SOURCE or edge.dst in seen:
                    continue
                seen.add(edge.dst)
                order.append(self.nodes[edge.dst])
                queue.append((edge.dst, depth + 1))
        return order

    def subgraph(self, node_id: str, depth: int = 2) -> dict:
        """Everything within `depth` hops, for the graph explorer screen."""
        if node_id not in self.nodes:
            return {"nodes": [], "edges": []}
        seen = {node_id}
        edges: list[Edge] = []
        frontier = [node_id]
        for _ in range(max(0, depth)):
            nxt = []
            for current in frontier:
                for edge, other in self.neighbours(current):
                    if edge not in edges:
                        edges.append(edge)
                    if other.id not in seen:
                        seen.add(other.id)
                        nxt.append(other.id)
            frontier = nxt
            if not frontier:
                break
        return {"root": node_id,
                "nodes": [self.nodes[n].to_dict() for n in seen],
                "edges": [e.to_dict() for e in edges]}

    def of_kind(self, kind: NodeKind) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind is kind]

    def stats(self) -> dict:
        by_kind: dict[str, int] = {}
        for n in self.nodes.values():
            by_kind[n.kind.value] = by_kind.get(n.kind.value, 0) + 1
        by_rel: dict[str, int] = {}
        for edges in self.out.values():
            for e in edges:
                by_rel[e.rel.value] = by_rel.get(e.rel.value, 0) + 1
        return {"nodes": len(self.nodes),
                "edges": sum(len(v) for v in self.out.values()),
                "by_kind": by_kind, "by_relation": by_rel}


# ---------------------------------------------------------------------------
# Building one from what the store holds
# ---------------------------------------------------------------------------

def build(aoi, changes=(), events=(), alerts=(), scenes=(),
          fields=(), correlations=()) -> Graph:
    """Assemble the graph for one area from records that already exist.

    Every node and edge here corresponds to something measured or recorded.
    Nothing is inferred, which is why there is no edge type for cause.
    """
    g = Graph()
    g.add(aoi.id, NodeKind.AREA, aoi.name,
          {"kind": aoi.kind.value, "area_km2": round(aoi.area_km2, 3)})

    for fld in fields:
        g.add(fld.id, NodeKind.FIELD, fld.name, {"use": fld.use})
        g.link(aoi.id, Relation.CONTAINS, fld.id)

    for scene in scenes:
        g.add(scene.id, NodeKind.SCENE, scene.id,
              {"at": scene.acquired_at.isoformat(), "gsd_m": scene.gsd_m,
               "cloud_pct": scene.cloud_pct})
        source = f"src-{scene.constellation.value}"
        g.add(source, NodeKind.SOURCE, scene.constellation.value)
        g.link(scene.id, Relation.DELIVERED_BY, source)
        sensor = f"sen-{scene.sensor.value}"
        g.add(sensor, NodeKind.SENSOR, scene.sensor.value)
        g.link(scene.id, Relation.CAPTURED_BY, sensor)

    for change in changes:
        g.add(change.id, NodeKind.CHANGE, change.change_type.value,
              {"area_m2": change.area_m2, "severity": change.severity.value,
               "confidence": change.confidence})
        g.link(change.id, Relation.LOCATED_IN, aoi.id)
        for scene_id in (change.before_scene_id, change.after_scene_id):
            if scene_id in g.nodes:
                g.link(change.id, Relation.DERIVED_FROM, scene_id)
        if change.model_version:
            model = f"mdl-{change.model_version}"
            g.add(model, NodeKind.MODEL, change.model_version)
            g.link(change.id, Relation.PRODUCED_BY, model)
        if change.evidence_id:
            g.add(change.evidence_id, NodeKind.EVIDENCE, change.evidence_id)
            g.link(change.evidence_id, Relation.EVIDENCES, change.id)

    previous: dict[str, str] = {}
    for event in events:
        g.add(event.id, NodeKind.EVENT, event.kind.value,
              {"looks": event.looks,
               "started_on": event.started_on.isoformat(),
               "trajectory": event.trajectory,
               "severity": event.severity.value})
        g.link(event.id, Relation.LOCATED_IN, aoi.id)
        for finding in event.findings:
            if finding.id in g.nodes:
                g.link(event.id, Relation.COMPOSES, finding.id)
        prior = previous.get(event.kind.value)
        if prior:
            g.link(event.id, Relation.PRECEDED_BY, prior)
        previous[event.kind.value] = event.id

    for alert in alerts:
        aid = alert.get("id") if isinstance(alert, dict) else alert.id
        title = alert.get("title") if isinstance(alert, dict) else alert.title
        finding = (alert.get("finding_id") if isinstance(alert, dict)
                   else getattr(alert, "finding_id", ""))
        evidence = (alert.get("evidence_id") if isinstance(alert, dict)
                    else getattr(alert, "evidence_id", ""))
        g.add(aid, NodeKind.ALERT, title or aid)
        if finding and finding in g.nodes:
            g.link(aid, Relation.RAISED_BY, finding)
        if evidence and evidence in g.nodes:
            g.link(aid, Relation.EVIDENCES, evidence)

    for corr in correlations:
        det = corr.detection
        g.add(det.id, NodeKind.DETECTION, "vessel",
              {"length_m": det.length_m, "sensor": det.sensor})
        g.link(det.id, Relation.LOCATED_IN, aoi.id)
        if corr.report is not None:
            rid = f"ais-{corr.report.mmsi}-{int(corr.report.at.timestamp())}"
            g.add(rid, NodeKind.VESSEL_REPORT,
                  corr.report.name or corr.report.mmsi,
                  {"mmsi": corr.report.mmsi,
                   "source": corr.report.source})
            #: MATCHES means a correlation was computed and passed its test.
            #: There is deliberately no edge for an uncorrelated detection --
            #: an absence of evidence is not a relationship.
            g.link(det.id, Relation.MATCHES, rid)
    return g
