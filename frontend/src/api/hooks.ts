import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { usePeriod } from "../state/period";
import type {
  AlertQueue,
  Audit,
  Change,
  CopilotAnswer,
  DetectedObject,
  Evidence,
  EvidencePath,
  EventsResponse,
  FieldsResponse,
  GraphStats,
  Health,
  ObservationsResponse,
  Projection,
  ProviderStatus,
  Report,
  ReviewStatus,
  Rule,
  Scene,
  SensingMatrix,
  SensingTarget,
  SiteDetail,
  SiteSummary,
  Subgraph,
  Timeline,
  Watchlist,
} from "./types";

// One hook per endpoint. Every hook that takes a period reads the global
// window, so all screens describe the same dates.

export const useHealth = () =>
  useQuery({ queryKey: ["health"], queryFn: () => api<Health>("/api/health"), refetchInterval: 60_000 });

export const useSites = () =>
  useQuery({
    queryKey: ["sites"],
    queryFn: () => api<{ sites: SiteSummary[] }>("/api/sites"),
    select: (d) => d.sites,
  });

export function useSite(id: string | undefined) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["site", id, window.from, window.to],
    queryFn: () => api<SiteDetail>(`/api/sites/${encodeURIComponent(id!)}`, { query: { from: window.from, to: window.to } }),
    enabled: !!id,
  });
}

export function useImagery(site: string | undefined) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["imagery", site, window.from, window.to],
    queryFn: () => api<{ site: string; scenes: Scene[] }>("/api/imagery", { query: { site, from: window.from, to: window.to } }),
    enabled: !!site,
    select: (d) => d.scenes,
  });
}

export function useChanges(site?: string) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["changes", site ?? "*", window.from, window.to],
    queryFn: () => api<{ changes: Change[] }>("/api/changes", { query: { site, from: window.from, to: window.to } }),
    select: (d) => d.changes,
  });
}

export const useObjects = (site: string | undefined, scene?: string) =>
  useQuery({
    queryKey: ["objects", site, scene],
    queryFn: () => api<{ site: string; objects: DetectedObject[] }>("/api/objects", { query: { site, scene } }),
    enabled: !!site,
    select: (d) => d.objects,
  });

export const useAlerts = (opts: { site?: string; status?: ReviewStatus | ""; maxPriority?: number } = {}) =>
  useQuery({
    queryKey: ["alerts", opts.site ?? "*", opts.status ?? "", opts.maxPriority ?? 4],
    queryFn: () =>
      api<AlertQueue>("/api/alerts", {
        query: { site: opts.site, status: opts.status || undefined, max_priority: opts.maxPriority ?? 4 },
      }),
    refetchInterval: 30_000,
  });

export function useTimeline(site: string | undefined) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["timeline", site, window.from, window.to],
    queryFn: () => api<Timeline>("/api/timeline", { query: { site, from: window.from, to: window.to } }),
    enabled: !!site,
  });
}

export function useEvents(site: string | undefined) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["events", site, window.from, window.to],
    queryFn: () =>
      api<EventsResponse>(`/api/sites/${encodeURIComponent(site!)}/events`, { query: { from: window.from, to: window.to } }),
    enabled: !!site,
  });
}

export function useObservations(site: string | undefined) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["observations", site, window.from, window.to],
    queryFn: () =>
      api<ObservationsResponse>(`/api/sites/${encodeURIComponent(site!)}/observations`, {
        query: { from: window.from, to: window.to },
      }),
    enabled: !!site,
  });
}

export const useProjection = (site: string | undefined, metric: string, to: string, confidence = 80) =>
  useQuery({
    queryKey: ["projection", site, metric, to, confidence],
    queryFn: () =>
      api<Projection>(`/api/sites/${encodeURIComponent(site!)}/projection`, { query: { metric, to, confidence } }),
    enabled: !!site && !!metric && !!to,
    retry: false,
  });

export function useGraphStats(site: string | undefined) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["graph", site, window.from, window.to],
    queryFn: () => api<GraphStats>(`/api/sites/${encodeURIComponent(site!)}/graph`, { query: { from: window.from, to: window.to } }),
    enabled: !!site,
  });
}

export function useSubgraph(site: string | undefined, node: string | undefined, depth = 2) {
  const { window } = usePeriod();
  return useQuery({
    queryKey: ["subgraph", site, node, depth, window.from, window.to],
    queryFn: () =>
      api<Subgraph>(`/api/sites/${encodeURIComponent(site!)}/graph`, {
        query: { node, depth, from: window.from, to: window.to },
      }),
    enabled: !!site && !!node,
    retry: false,
  });
}

export const useEvidencePath = (node: string | undefined, site: string | undefined) =>
  useQuery({
    queryKey: ["evidence-path", node, site],
    queryFn: () => api<EvidencePath>(`/api/evidence-path/${encodeURIComponent(node!)}`, { query: { aoi_id: site } }),
    enabled: !!node && !!site,
    retry: false,
  });

export const useEvidence = (id: string | undefined) =>
  useQuery({
    queryKey: ["evidence", id],
    queryFn: () => api<Evidence>(`/api/evidence/${encodeURIComponent(id!)}`),
    enabled: !!id,
  });

export const useWatchlists = () =>
  useQuery({
    queryKey: ["watchlists"],
    queryFn: () => api<{ watchlists: Watchlist[] }>("/api/watchlists"),
    select: (d) => d.watchlists,
  });

export const useRules = () =>
  useQuery({ queryKey: ["rules"], queryFn: () => api<{ rules: Rule[] }>("/api/rules"), select: (d) => d.rules });

export const useReports = () =>
  useQuery({ queryKey: ["reports"], queryFn: () => api<{ reports: Report[] }>("/api/reports"), select: (d) => d.reports });

export const useAudit = (enabled: boolean, limit = 200) =>
  useQuery({ queryKey: ["audit", limit], queryFn: () => api<Audit>("/api/audit", { query: { limit } }), enabled, retry: false });

export const useSensing = (task: string) =>
  useQuery({ queryKey: ["sensing", task], queryFn: () => api<SensingMatrix>("/api/sensing", { query: { task } }) });

export const useSensingTarget = (task: string, target: string) =>
  useQuery({
    queryKey: ["sensing", task, target],
    queryFn: () => api<SensingTarget>("/api/sensing", { query: { task, target } }),
    enabled: !!target,
    retry: false,
  });

export const useFields = (site: string | undefined) =>
  useQuery({
    queryKey: ["fields", site],
    queryFn: () => api<FieldsResponse>(`/api/sites/${encodeURIComponent(site!)}/fields`),
    enabled: !!site,
  });

export const useProviders = () =>
  useQuery({ queryKey: ["providers"], queryFn: () => api<ProviderStatus>("/api/providers"), retry: false });

// --- writes ---------------------------------------------------------------

export function useReview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status, note }: { id: string; status: ReviewStatus; note: string }) =>
      api(`/api/reviews/${encodeURIComponent(id)}`, { method: "POST", body: { status, note } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts"] });
      qc.invalidateQueries({ queryKey: ["changes"] });
      qc.invalidateQueries({ queryKey: ["events"] });
    },
  });
}

export function useCreateSite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; kind: string; country: string; description: string; boundary: unknown; id?: string }) =>
      api<{ site: SiteSummary }>("/api/sites", { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sites"] }),
  });
}

export function useCreateWatchlist() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; aoi_ids: string[]; priority: string }) =>
      api("/api/watchlists", { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlists"] }),
  });
}

export function useCreateField(site: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; use: string; notes: string; boundary: unknown }) =>
      api(`/api/sites/${encodeURIComponent(site)}/fields`, { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["fields", site] }),
  });
}

export function useRetireField(site: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/api/fields/${encodeURIComponent(id)}/retire`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["fields", site] }),
  });
}

export const useCopilot = () =>
  useMutation({
    mutationFn: (question: string) => api<CopilotAnswer>("/api/copilot", { method: "POST", body: { question } }),
  });
