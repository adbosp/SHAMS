import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Conversation, Preset, WorkspaceRow } from "./types";

export type Theme = "system" | "light" | "dark";

function readLS(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function writeLS(key: string, v: string) {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* private mode */
  }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => readLS("shams.theme", "system") as Theme);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && mq.matches);
      document.documentElement.classList.toggle("dark", dark);
    };
    apply();
    mq.addEventListener("change", apply);
    writeLS("shams.theme", theme);
    return () => mq.removeEventListener("change", apply);
  }, [theme]);
  return [theme, setTheme];
}

export function usePersisted(key: string, fallback: string): [string, (v: string) => void] {
  const [v, setV] = useState(() => readLS(key, fallback));
  return [v, (nv: string) => {
    setV(nv);
    writeLS(key, nv);
  }];
}

export function useData<T>(path: string | null, deps: unknown[] = [], every?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    load();
    if (!every) return;
    const t = setInterval(load, every);
    return () => clearInterval(t);
  }, [load, every]);
  return { data, error, loading, reload: load, setData };
}

export type Shared = {
  conversations: Conversation[];
  reloadConversations: () => Promise<void>;
  presets: Preset[];
  reloadPresets: () => Promise<void>;
  workspaces: WorkspaceRow[];
  reloadWorkspaces: () => Promise<void>;
};

export function useShared(ready: boolean): Shared {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([]);
  const reloadConversations = useCallback(async () => {
    try {
      setConversations((await api.get<{ conversations: Conversation[] }>("/api/desktop/conversations")).conversations);
    } catch {
      /* backend not ready */
    }
  }, []);
  const reloadPresets = useCallback(async () => {
    try {
      setPresets((await api.get<{ presets: Preset[] }>("/api/desktop/presets")).presets);
    } catch {
      /* backend not ready */
    }
  }, []);
  const reloadWorkspaces = useCallback(async () => {
    try {
      setWorkspaces((await api.get<{ workspaces: WorkspaceRow[] }>("/api/desktop/workspaces")).workspaces);
    } catch {
      /* backend not ready */
    }
  }, []);
  useEffect(() => {
    if (!ready) return;
    reloadConversations();
    reloadPresets();
    reloadWorkspaces();
  }, [ready, reloadConversations, reloadPresets, reloadWorkspaces]);
  return { conversations, reloadConversations, presets, reloadPresets, workspaces, reloadWorkspaces };
}

export const KIND_LABEL: Record<string, string> = {
  chat: "Chat", image: "Ảnh", video: "Video", scene3d: "Cảnh 3D", image_to_3d: "Ảnh → 3D", audio: "Âm thanh",
};

export function timeAgo(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "vừa xong";
  if (s < 3600) return `${Math.floor(s / 60)} phút`;
  if (s < 86400) return `${Math.floor(s / 3600)} giờ`;
  return new Date(ts * 1000).toLocaleDateString("vi-VN");
}
