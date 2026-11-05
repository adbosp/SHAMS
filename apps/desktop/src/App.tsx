import { useCallback, useEffect, useState } from "react";
import {
  Bot, Boxes, Cpu, FolderGit2, History, Image as ImageIcon, MessageSquarePlus, Moon, Network, Settings as Cog, Sun,
  SunMoon, Zap,
} from "lucide-react";
import { api, backendInfo, inTauri, setBackend } from "./lib/api";
import { useShared, useTheme } from "./lib/store";
import type { Route } from "./lib/types";
import ChatView from "./pages/ChatView";
import HistoryPage from "./pages/HistoryPage";
import DirectPage from "./pages/DirectPage";
import AutonomousPage from "./pages/AutonomousPage";
import ProjectsPage from "./pages/ProjectsPage";
import MediaPage from "./pages/MediaPage";
import ModelsPage from "./pages/ModelsPage";
import SettingsPage from "./pages/SettingsPage";
import Sidebar from "./components/Sidebar";
import StatusBar from "./components/StatusBar";

type Health = { ok: boolean; version: string; workspace: string; pid: number };

export const NAV = [
  { page: "chat", label: "New Chat", icon: MessageSquarePlus },
  { page: "history", label: "Chat History", icon: History },
  { page: "direct", label: "Direct AI", icon: Zap },
  { page: "autonomous", label: "Autonomous Workflow", icon: Network },
  { page: "projects", label: "Projects", icon: FolderGit2 },
  { page: "media", label: "Media Gallery", icon: ImageIcon },
  { page: "models", label: "Model Manager", icon: Boxes },
  { page: "settings", label: "Settings", icon: Cog },
] as const;

export default function App() {
  const [theme, setTheme] = useTheme();
  const [route, setRoute] = useState<Route>({ page: "chat" });
  const [health, setHealth] = useState<Health | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const shared = useShared(!!health);

  const connect = useCallback(async () => {
    setBootError(null);
    for (let i = 0; i < 120; i++) {
      try {
        const b = await backendInfo(true);
        if (b.url) {
          const h = await api.get<Health>("/api/health");
          setHealth(h);
          return;
        }
      } catch (e: any) {
        if (i === 119) setBootError(e.message || String(e));
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }, []);

  useEffect(() => {
    connect();
    if (!inTauri()) return;
    let un: (() => void) | undefined;
    import("@tauri-apps/api/event").then(({ listen }) =>
      listen<{ url: string; token: string | null; managed: boolean }>("backend://ready", (e) => {
        setBackend(e.payload);
        connect();
      }).then((f) => (un = f)),
    );
    return () => un?.();
  }, [connect]);

  // health check every 10 s: show a banner when the Core Service stops answering
  useEffect(() => {
    if (!health) return;
    const t = setInterval(async () => {
      try {
        setHealth(await api.get<Health>("/api/health"));
      } catch {
        setHealth(null);
        connect();
      }
    }, 10000);
    return () => clearInterval(t);
  }, [health, connect]);

  const nextTheme = () => setTheme(theme === "system" ? "dark" : theme === "dark" ? "light" : "system");
  const ThemeIcon = theme === "system" ? SunMoon : theme === "dark" ? Moon : Sun;

  if (!health) return <Boot error={bootError} retry={connect} />;

  return (
    <div className="flex h-full flex-col">
      <div className="flex min-h-0 flex-1">
        <Sidebar route={route} go={setRoute} shared={shared}
          footer={
            <button className="btn-ghost w-full justify-start" onClick={nextTheme} title="Giao diện sáng / tối">
              <ThemeIcon size={16} /> {theme === "system" ? "Theo hệ thống" : theme === "dark" ? "Tối" : "Sáng"}
            </button>
          } />
        <main className="min-w-0 flex-1 overflow-hidden">
          {route.page === "chat" && <ChatView key={route.id || "new"} id={route.id} shared={shared} go={setRoute} />}
          {route.page === "history" && <HistoryPage shared={shared} go={setRoute} />}
          {route.page === "direct" && <DirectPage shared={shared} go={setRoute} />}
          {route.page === "autonomous" && <AutonomousPage />}
          {route.page === "projects" && <ProjectsPage shared={shared} go={setRoute} initial={route.id} />}
          {route.page === "media" && <MediaPage go={setRoute} />}
          {route.page === "models" && <ModelsPage shared={shared} />}
          {route.page === "settings" && <SettingsPage theme={theme} setTheme={setTheme} health={health} />}
        </main>
      </div>
      <StatusBar health={health} />
    </div>
  );
}

function Boot({ error, retry }: { error: string | null; retry: () => void }) {
  const [log, setLog] = useState<string | null>(null);
  useEffect(() => {
    if (!error || !inTauri()) return;
    import("@tauri-apps/api/core").then(({ invoke }) => invoke<string>("backend_log").then(setLog).catch(() => undefined));
  }, [error]);
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="card w-full max-w-lg p-6 text-center">
        <img src="/logo.png" className="mx-auto mb-3 h-12 w-12" alt="" />
        <div className="text-lg font-semibold">SHAMS</div>
        {!error ? (
          <div className="mt-2 flex items-center justify-center gap-2 text-muted">
            <Cpu size={16} className="animate-pulse" /> Đang khởi động SHAMS Core Service...
          </div>
        ) : (
          <div className="mt-3 text-left">
            <div className="text-bad">Không kết nối được backend: {error}</div>
            <p className="mt-2 text-sm text-muted">
              Kiểm tra cấu hình Python/thư mục SHAMS trong <code>%APPDATA%\shams-desktop\config.json</code>, hoặc chạy
              <code> shams desktop-backend</code> thủ công.
            </p>
            {log && <pre className="mt-3 max-h-60 overflow-auto rounded bg-sunk p-2 text-xs">{log}</pre>}
            <button className="btn-primary mt-3" onClick={retry}><Bot size={16} /> Thử lại</button>
          </div>
        )}
      </div>
    </div>
  );
}
