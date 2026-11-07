import { useEffect, useState } from "react";
import { Archive, Code2, MessageSquare, Pin, Search, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { timeAgo, type Shared } from "../lib/store";
import type { Conversation, Route } from "../lib/types";

export default function HistoryPage({ shared, go }: { shared: Shared; go: (r: Route) => void }) {
  const [q, setQ] = useState("");
  const [archived, setArchived] = useState(false);
  const [rows, setRows] = useState<Conversation[]>([]);
  const load = async () => {
    const r = await api.get<{ conversations: Conversation[] }>(
      `/api/desktop/conversations?q=${encodeURIComponent(q)}&archived=${archived}`);
    setRows(r.conversations);
  };
  useEffect(() => {
    const t = setTimeout(load, 200);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, archived]);
  const patch = async (c: Conversation, f: Partial<Conversation>) => {
    await api.patch(`/api/desktop/conversations/${c.id}`, f);
    load();
    shared.reloadConversations();
  };
  const ws = (id: string | null) => shared.workspaces.find((w) => w.id === id)?.name;
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-6">
        <h1 className="mb-4 text-xl font-semibold">Chat History</h1>
        <div className="mb-3 flex items-center gap-2">
          <div className="relative flex-1">
            <Search size={15} className="absolute left-2.5 top-2.5 text-muted" />
            <input className="input pl-8" placeholder="Tìm theo tiêu đề hoặc nội dung..." value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <label className="flex items-center gap-1.5 text-sm text-muted">
            <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} /> Gồm đã lưu trữ
          </label>
        </div>
        <div className="card divide-y divide-line">
          {rows.length === 0 && <div className="p-6 text-center text-muted">Chưa có cuộc trò chuyện nào.</div>}
          {rows.map((c) => (
            <div key={c.id} className="group flex items-center gap-3 px-3 py-2.5 hover:bg-sunk">
              {c.mode === "agent" ? <Code2 size={16} className="text-accent" /> : <MessageSquare size={16} className="text-muted" />}
              <button className="min-w-0 flex-1 text-left" onClick={() => go({ page: "chat", id: c.id })}>
                <div className="truncate font-medium">{c.title}</div>
                <div className="truncate text-xs text-muted">
                  {c.preset || "—"} · {c.message_count ?? 0} tin nhắn{ws(c.workspace_id) ? ` · ${ws(c.workspace_id)}` : ""} · {timeAgo(c.updated)}
                </div>
              </button>
              <div className="hidden gap-1 group-hover:flex">
                <button className="btn-ghost" title="Ghim" onClick={() => patch(c, { pinned: c.pinned ? 0 : 1 })}><Pin size={14} /></button>
                <button className="btn-ghost" title="Lưu trữ" onClick={() => patch(c, { archived: c.archived ? 0 : 1 })}><Archive size={14} /></button>
                <button className="btn-ghost" title="Xóa" onClick={async () => {
                  if (!confirm(`Xóa "${c.title}"?`)) return;
                  await api.del(`/api/desktop/conversations/${c.id}`);
                  load();
                  shared.reloadConversations();
                }}><Trash2 size={14} /></button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
