// Types for the TerraShield HTTP API, written against responses captured from
// a running server (`terrashield serve`). The API contract is the durable
// part of the product; these types are its client-side mirror. When a field is
// added on the server it is optional here until the console uses it.

export type Severity = "low" | "medium" | "high" | "critical";
export type ReviewStatus = "pending" | "confirmed" | "rejected" | "escalated";
export type Role = "viewer" | "analyst" | "supervisor" | "admin";
export type LonLat = [number, number];

export interface ApiErrorBody {
  error: string;
  hint?: string;
}

export interface Account {
  id: string;
  org_id: string;
  email: string;
  name: string;
  role: Role;
  active: boolean;
}

export interface Session {
  token: string;
  expires_at: string;
  account: Account;
}

export interface Me {
  actor: string;
  organisation: string;
  role: Role;
  permissions: string[];
}

export interface Health {
  status: string;
  database: string;
  schema_version: number;
  organisation: string;
  counts: Record<string, number>;
  audit_chain_intact: boolean;
  first_bad_audit_entry: number | null;
}

export interface SiteSummary {
  id: string;
  name: string;
  kind: string;
  country: string;
  area_km2: number;
  centroid: LonLat;
  bbox: [number, number, number, number];
  fingerprint: string;
  description: string;
}

export interface SiteDetail {
  site: {
    id: string;
    name: string;
    kind: string;
    area_km2: number;
    boundary: LonLat[];
    fingerprint: string;
  };
  period: Period;
  coverage: { acquired: number; usable: number };
  changes: number;
  peak_anomaly_score: number;
  alerts: number;
}

export interface Period {
  from: string;
  to: string;
}

export interface Scene {
  id: string;
  constellation: string;
  sensor: string;
  acquired_on: string;
  gsd_m: number;
  cloud_pct: number;
  sun_elevation_deg: number;
  orbit: string;
  usable: boolean;
  unusable_reason: string;
}

export interface Change {
  id: string;
  aoi_id: string;
  type: string;
  detected_on: string;
  area_m2: number;
  confidence: number;
  severity: Severity;
  explanation: string;
  geometry: LonLat[];
  before_scene_id: string;
  after_scene_id: string;
  evidence_id: string;
  review_status: ReviewStatus;
}

export interface DetectedObject {
  id: string;
  class: string;
  confidence: number;
  lon: number;
  lat: number;
  extent_m: number;
  scene_id: string;
  observed_at: string;
  model_version: string;
}

export interface Alert {
  id: string;
  org_id: string;
  aoi_id: string;
  rule_id: string;
  title: string;
  severity: Severity;
  priority: 1 | 2 | 3 | 4;
  created_at: string;
  summary: string;
  evidence_id: string;
  dedup_key: string;
  occurrences: number;
  review_status: ReviewStatus;
  reviewed_by: string;
  reviewed_at: string | null;
  review_note: string;
  payload: {
    rule_name?: string;
    change_event_ids?: string[];
    anomaly_ids?: string[];
    matched_conditions?: string[];
    rule?: string;
    matched_rules?: string[];
    risk?: Record<string, number | string> | null;
    held_for_confirmation?: string;
    channels?: string[];
    [k: string]: unknown;
  };
}

export interface AlertQueue {
  alerts: Alert[];
  queue: {
    total: number;
    priority_1: number;
    priority_2: number;
    priority_3: number;
    priority_4: number;
  };
}

export interface Timeline {
  site: string;
  period: Period;
  acquisitions: {
    date: string;
    constellation: string;
    sensor: string;
    usable: boolean;
    cloud_pct: number;
    reason: string;
  }[];
  changes: { date: string; type: string; area_m2: number; severity: Severity; id: string }[];
  anomaly_scores: { date: string; score: number; confidence: number }[];
}

export interface Artefact {
  kind: string;
  ref: string;
  sha256: string;
  note: string;
}

export interface Evidence {
  id: string;
  aoi_id: string;
  finding_id: string;
  finding_kind: string;
  created_at: string;
  finding_at: string;
  sha256: string;
  artefacts: Artefact[];
  measurements: Record<string, number | string>;
  gaps: string[];
  narrative: string;
  [k: string]: unknown;
}

export interface Watchlist {
  id: string;
  name: string;
  priority: Severity;
  aoi_ids: string[];
}

export interface Rule {
  id: string;
  org_id: string;
  name: string;
  enabled: boolean;
  conditions: { field: string; op: string; value: unknown }[];
  severity_floor: Severity;
  aoi_ids: string[];
  aoi_kinds: string[];
  channels: string[];
  suppress_days: number;
  created_by: string;
  description: string;
}

export interface Report {
  id?: string;
  kind?: string;
  title?: string;
  created_at?: string;
  body?: string;
  [k: string]: unknown;
}

export interface AuditEntry {
  seq?: number;
  at?: string;
  actor?: string;
  action?: string;
  target?: string;
  [k: string]: unknown;
}

export interface Audit {
  chain_intact: boolean;
  first_bad_entry: number | null;
  entries: AuditEntry[];
}

export interface FusedEvent {
  id: string;
  aoi_id: string;
  kind: string;
  lon: number;
  lat: number;
  started_on: string;
  last_seen_on: string;
  duration_days: number;
  looks: number;
  confirmed: boolean;
  trajectory: string;
  severity: Severity;
  confidence: number;
  peak_area_m2: number;
  latest_area_m2: number;
  composed_of: string[];
  finding_ids: string[];
  evidence_ids: string[];
  headline: string;
  novelty: number;
}

export interface EventsResponse {
  aoi_id: string;
  period: Period;
  summary: {
    events: number;
    findings: number;
    reduction: number;
    confirmed: number;
    single_look: number;
    by_kind: Record<string, number>;
  };
  events: FusedEvent[];
}

export interface GraphNode {
  id: string;
  kind: string;
  label: string;
  attrs?: Record<string, unknown>;
}
export interface GraphEdge {
  from: string;
  rel: string;
  to: string;
}
export interface GraphStats {
  aoi_id: string;
  stats: {
    nodes: number;
    edges: number;
    by_kind: Record<string, number>;
    by_relation: Record<string, number>;
  };
}
export interface Subgraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  [k: string]: unknown;
}
export interface EvidencePath {
  root: string;
  path: GraphNode[];
}

export interface SensingRow {
  target: string;
  smallest_m: number;
  object_class: string;
  possible_from_orbit: boolean;
  cheapest: string | null;
  [platform: string]: string | number | boolean | null;
}
export interface SensingMatrix {
  task: string;
  matrix: SensingRow[];
}
export interface SensingVerdict {
  platform?: string;
  possible?: boolean;
  reason?: string;
  [k: string]: unknown;
}
export interface SensingTarget {
  task: string;
  target: string;
  explanation: string;
  platforms: string[];
  verdicts: SensingVerdict[];
}

export interface Field {
  id: string;
  aoi_id: string;
  name: string;
  use: string;
  notes: string;
  hectares: number;
  area_km2: number;
  centroid: LonLat;
  bbox: number[];
  boundary: LonLat[];
  fingerprint: string;
  active: boolean;
}
export interface FieldsResponse {
  aoi_id: string;
  count: number;
  hectares: number;
  fields: Field[];
}

export interface CopilotAnswer {
  question: string;
  intent: string;
  answer: string;
  rows: Record<string, unknown>[];
  citations: { kind: string; ref: string; detail: string }[];
  unsupported: string;
}

export interface ObservationsResponse {
  aoi_id: string;
  period: Period;
  metrics: string[];
  series: Record<string, { date: string; value: number; quality: number; scene_id: string }[]>;
}

export interface Projection {
  aoi_id: string;
  metric: string;
  is_projection: boolean;
  band?: { low: number; high: number; confidence_pct: number };
  horizon?: string;
  days_ahead?: number;
  headline?: string;
  reason?: string;
  assumption?: string;
  [k: string]: unknown;
}

export interface ProviderStatus {
  selected: string;
  select_with: string;
  synthetic_available: boolean;
  providers: {
    name: string;
    title: string;
    selected: boolean;
    free: boolean;
    needs_account: boolean;
    ready: boolean;
    missing: string[];
    constellations: string[];
    notes: string;
  }[];
}

// --- Live tracking (phase 1) ------------------------------------------------

export type TrackKind = "person" | "vehicle" | "drone" | "unknown";
export type ZoneRule = "line" | "no_entry" | "loiter";

export interface Sensor {
  id: string;
  name: string;
  kind: string;
  lon: number | null;
  lat: number | null;
  range_m: number;
  simulated: boolean;
  reports: number;
  last_heard_at: string | null;
  status: "online" | "silent" | "never heard";
}

export interface LiveTrack {
  id: string;
  uid: string;
  sensor_id: string;
  kind: TrackKind;
  lon: number;
  lat: number;
  speed_mps: number | null;
  heading_deg: number | null;
  first_seen: string;
  last_seen: string;
  points: number;
  age_s: number;
  state: "live" | "lost";
  simulated: boolean;
  trail: LonLat[];
}

export interface Zone {
  id: string;
  name: string;
  rule: ZoneRule;
  geometry: LonLat[];
  kinds: TrackKind[];
  loiter_s: number;
  simulated: boolean;
  active: boolean;
}

export interface TrackAlert {
  seq: number;
  id: string;
  zone_id: string;
  zone_name: string;
  track_id: string;
  sensor_id: string;
  rule: ZoneRule;
  kind: TrackKind;
  at: string;
  lon: number;
  lat: number;
  summary: string;
  trail: [number, number, string][];
  simulated: boolean;
  status: ReviewStatus;
  reviewed_by: string;
  reviewed_at: string | null;
  note: string;
}

export interface LiveResponse {
  as_of: string;
  window_s: number;
  tracks: LiveTrack[];
  zones: Zone[];
  sensors: Sensor[];
  alerts: TrackAlert[];
  alert_cursor: number;
  pending_alerts: number;
  lost_after_s: number;
  retention_hours: number;
}

export interface TrackHistory {
  id: string;
  uid: string;
  sensor_id: string;
  kind: TrackKind;
  first_seen: string;
  last_seen: string;
  simulated: boolean;
  points: { t: string; lon: number; lat: number; speed_mps: number | null; heading_deg: number | null }[];
}

export interface ReplayResponse {
  from: string;
  to: string;
  truncated: boolean;
  points: { track_id: string; t: string; lon: number; lat: number; kind: TrackKind; simulated: boolean }[];
}
