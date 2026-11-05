import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, Folder, MessageSquare, Pin } from "lucide-react";
import { NAV } from "../App";
import type { Shared } from "../lib/store";
import type { Conversation, Route } from "../lib/types";

export default function Sidebar({ route, go, shared, footer }: {
  route: Route; go: (r: Route) => void; shared: Shared; footer: ReactNode;
}) {
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  // Codex-style: threads grouped under their local project folder; loose chats at the bottom
  const groups = useMemo(() => {
    const by: Record<string, Conversation[]> = {};
    for (const c of shared.conversations.slice(0, 80)) (by[c.workspace_id || ""] ||= []).push(c);
    const named = shared.workspaces.filter((w) => by[w.id]).map((w) => ({ id: w.id, name: w.name, items: by[w.id] }));
    return { named, loose: by[""] || [] };
  }, [shared.conversations, shared.workspaces]);

  const active = route.page === "chat" ? route.id : undefined;
  const item = (c: Conversation) => (
    <button key={c.id} onClick={() => go({ page: "chat", id: c.id })}
      className={`group flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] ${
        active === c.id ? "bg-sunk text-fg" : "text-muted hover:bg-sunk hover:text-fg"}`}>
      {c.pinned ? <Pin size={13} className="shrink-0" /> : <MessageSquare size={13} className="shrink-0 opacity-60" />}
      <span className="truncate">{c.title}</span>
      {c.mode === "agent" && <span className="ml-auto rounded bg-accent/10 px-1 text-[10px] text-accent">agent</span>}
    </button>
  );

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-panel">
      <div className="flex items-center gap-2 px-4 pb-2 pt-3">
        <img src="/logo.png" className="h-6 w-6" alt="" />
        <span className="font-semibold tracking-wide">SHAMS</span>
      </div>
      <nav className="px-2">
        {NAV.map(({ page, label, icon: Icon }) => {
          const on = route.page === page && !(page === "chat" && route.page === "chat" && route.id);
          return (
            <button key={page} onClick={() => go({ page } as Route)}
              className={`flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-sm ${
                on ? "bg-sunk font-medium text-fg" : "text-muted hover:bg-sunk hover:text-fg"}`}>
              <Icon size={16} /> {label}
            </button>
          );
        })}
      </nav>
      <div className="mt-3 min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {groups.named.map((g) => (
          <div key={g.id} className="mb-1">
            <button onClick={() => setClosed({ ...closed, [g.id]: !closed[g.id] })}
              className="flex w-full items-center gap-1.5 px-2 py-1 text-xs font-medium text-muted hover:text-fg">
              {closed[g.id] ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
              <Folder size={12} /> <span className="truncate">{g.name}</span>
            </button>
            {!closed[g.id] && <div className="pl-2">{g.items.map(item)}</div>}
          </div>
        ))}
        {groups.loose.length > 0 && (
          <div>
            <div className="px-2 py-1 text-xs font-medium text-muted">Trò chuyện</div>
            {groups.loose.map(item)}
          </div>
        )}
      </div>
      <div className="border-t border-line p-2">{footer}</div>
    </aside>
  );
}
