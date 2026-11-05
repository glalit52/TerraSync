import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { useAuth } from "./state/auth";
import { Login } from "./pages/Login";

// Pages are split per route so the first paint after sign-in carries only the
// command screen; the orbit propagator and the graph explorer load on demand.
const Command = lazy(() => import("./pages/Command"));
const MapPage = lazy(() => import("./pages/MapPage"));
const Alerts = lazy(() => import("./pages/Alerts"));
const Sites = lazy(() => import("./pages/Sites"));
const SiteDetail = lazy(() => import("./pages/SiteDetail"));
const Changes = lazy(() => import("./pages/Changes"));
const EvidencePage = lazy(() => import("./pages/Evidence"));
const GraphPage = lazy(() => import("./pages/Graph"));
const Copilot = lazy(() => import("./pages/Copilot"));
const LiveTracks = lazy(() => import("./pages/LiveTracks"));
const AssetIntel = lazy(() => import("./pages/AssetIntel"));
const Maritime = lazy(() => import("./pages/Maritime"));
const OrbitPage = lazy(() => import("./pages/Orbit"));
const Watchlists = lazy(() => import("./pages/Watchlists"));
const Rules = lazy(() => import("./pages/Rules"));
const Reports = lazy(() => import("./pages/Reports"));
const Sensing = lazy(() => import("./pages/Sensing"));
const Sources = lazy(() => import("./pages/Sources"));
const Integrations = lazy(() => import("./pages/Integrations"));
const Admin = lazy(() => import("./pages/Admin"));
const NotFound = lazy(() => import("./pages/NotFound"));

export function App() {
  const { token, me, loading } = useAuth();
  if (loading) return <Loading label="Restoring session" />;
  if (!token || !me) return <Login />;
  return (
    <Suspense fallback={<Loading />}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Command />} />
          <Route path="map" element={<MapPage />} />
          <Route path="alerts" element={<Alerts />} />
          <Route path="sites" element={<Sites />} />
          <Route path="sites/:id" element={<SiteDetail />} />
          <Route path="changes" element={<Changes />} />
          <Route path="evidence/:id" element={<EvidencePage />} />
          <Route path="graph" element={<GraphPage />} />
          <Route path="copilot" element={<Copilot />} />
          <Route path="live" element={<LiveTracks />} />
          <Route path="assets" element={<AssetIntel />} />
          <Route path="maritime" element={<Maritime />} />
          <Route path="orbit" element={<OrbitPage />} />
          <Route path="watchlists" element={<Watchlists />} />
          <Route path="rules" element={<Rules />} />
          <Route path="reports" element={<Reports />} />
          <Route path="sensing" element={<Sensing />} />
          <Route path="sources" element={<Sources />} />
          <Route path="integrations" element={<Integrations />} />
          <Route path="admin" element={<Admin />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
