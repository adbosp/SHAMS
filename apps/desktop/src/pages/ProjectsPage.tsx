import { useEffect, useState } from "react";
import { ChevronRight, Code2, File, Folder, FolderOpen, FolderPlus, GitBranch, Plus, Save, Trash2, X } from "lucide-react";
import { api, inTauri } from "../lib/api";
import { useData, type Shared } from "../lib/store";
import type { Conversation, Route, WorkspaceRow } from "../lib/types";
import { DiffView } from "../components/Blocks";

type Tree = { path: string; items: { name: string; path: string; dir: boolean; size: number | null }[] };

async function pickFolder(): Promise<string | null> {
  if (!inTauri()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const r = await open({ directory: true, multiple: false, title: "Chọn thư mục Project" });
  return typeof r === "string" ? r : null;
}

/** Projects = a local folder on this PC + its environment (env vars, shell, Python/venv, notes for the agent). */
export default function ProjectsPage({ shared, go, initial }: { shared: Shared; go: (r: Route) => void; initial?: string }) {
  const [sel, setSel] = useState<string | null>(initial || null);
  const [adding, setAdding] = useState(false);
  const info = useData<{ projects_root: string }>("/api/desktop/workspaces");
  useEffect(() => {
    if (!sel && shared.workspaces[0]) setSel(shared.workspaces[0].id);
  }, [shared.workspaces, sel]);
  const ws = shared.workspaces.find((w) => w.id === sel);
  return (
    <div className="flex h-full">
      <div className="w-72 shrink-0 overflow-y-auto border-r border-line p-3">
        <div className="mb-2 flex items-center justify-between">
          <h1 className="font-semibold">Projects</h1>
          <button className="btn py-1 text-xs" onClick={() => setAdding(true)}><Plus size={14} /> Thêm</button>
        </div>
        {shared.workspaces.length === 0 && <div className="text-sm text-muted">Chưa có Project. Thêm một thư mục trên máy.</div>}
        {shared.workspaces.map((w) => (
          <button key={w.id} onClick={() => setSel(w.id)}
            className={`mb-1 w-full rounded-md px-2 py-1.5 text-left ${sel === w.id ? "bg-sunk" : "hover:bg-sunk"}`}>
            <div className="flex items-center gap-1.5 text-sm font-medium"><Folder size={14} /> {w.name}
              {w.git && <GitBranch size={12} className="text-muted" />}{w.exists === false && <span className="text-bad text-xs">mất</span>}</div>
            <div className="truncate text-[11px] text-muted">{w.path}</div>
          </button>
        ))}
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto">
        {adding ? <AddForm root={info.data?.projects_root || ""} onDone={async (id) => {
          setAdding(false);
          await shared.reloadWorkspaces();
          if (id) setSel(id);
        }} /> : ws ? <Detail key={ws.id} ws={ws} shared={shared} go={go} /> : null}
      </div>
    </div>
  );
}

function AddForm({ root, onDone }: { root: string; onDone: (id?: string) => void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [create, setCreate] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="max-w-xl p-6">
      <h2 className="mb-3 text-lg font-semibold">Thêm Project</h2>
      <label className="mb-3 block text-sm">Tên
        <input className="input mt-1" value={name} onChange={(e) => setName(e.target.value)} placeholder="Ví dụ: Web shop" />
      </label>
      <label className="mb-1 block text-sm">Thư mục trên máy
        <div className="mt-1 flex gap-2">
          <input className="input" value={path} onChange={(e) => setPath(e.target.value)} placeholder={`Để trống = tạo trong ${root}`} />
          {inTauri() && <button className="btn" onClick={async () => {
            const p = await pickFolder();
            if (p) {
              setPath(p);
              if (!name) setName(p.split(/[\\/]/).pop() || "");
            }
          }}><FolderOpen size={14} /> Chọn</button>}
        </div>
      </label>
      <label className="mb-4 mt-2 flex items-center gap-2 text-sm text-muted">
        <input type="checkbox" checked={create || !path} disabled={!path} onChange={(e) => setCreate(e.target.checked)} />
        Tạo thư mục nếu chưa có
      </label>
      {err && <div className="mb-2 text-sm text-bad">{err}</div>}
      <div className="flex gap-2">
        <button className="btn-primary" onClick={async () => {
          try {
            const w = await api.post<WorkspaceRow>("/api/desktop/workspaces", { name, path, create_folder: create || !path });
            onDone(w.id);
          } catch (e: any) {
            setErr(e.message);
          }
        }}><FolderPlus size={14} /> Thêm</button>
        <button className="btn" onClick={() => onDone()}>Hủy</button>
      </div>
    </div>
  );
}

function Detail({ ws, shared, go }: { ws: WorkspaceRow; shared: Shared; go: (r: Route) => void }) {
  const [env, setEnv] = useState<[string, string][]>(Object.entries(ws.env || {}));
  const [shell, setShell] = useState(ws.shell || "");
  const [python, setPython] = useState(ws.python || "");
  const [notes, setNotes] = useState(ws.notes || "");
  const [msg, setMsg] = useState<string | null>(null);
  const [path, setPath] = useState(".");
  const [file, setFile] = useState<{ path: string; text: string } | null>(null);
  const [tab, setTab] = useState<"files" | "git" | "env">("files");
  const tree = useData<Tree>(`/api/desktop/workspaces/${ws.id}/tree?path=${encodeURIComponent(path)}`, [path]);
  const git = useData<{ git: boolean; branch?: string; status?: string; diff?: string }>(tab === "git" ? `/api/desktop/workspaces/${ws.id}/git` : null, [tab]);
  const convs = shared.conversations.filter((c) => c.workspace_id === ws.id);

  const save = async () => {
    const bad = env.find(([k]) => /(KEY|TOKEN|SECRET|PASSWORD)/i.test(k));
    if (bad && !confirm(`Biến ${bad[0]} trông như bí mật. Biến môi trường Project được lưu dạng chữ thường trong SQLite trên máy. Vẫn lưu?`)) return;
    await api.patch(`/api/desktop/workspaces/${ws.id}`, {
      env: Object.fromEntries(env.filter(([k]) => k.trim())), shell: shell || null, python: python || null, notes,
    });
    setMsg("Đã lưu");
    shared.reloadWorkspaces();
    setTimeout(() => setMsg(null), 1500);
  };
  const newAgent = async () => {
    const preset = shared.presets.find((p) => p.status === "ok" && p.kind === "chat" && /code|coder/i.test(p.id + (p.model || "")))
      || shared.presets.find((p) => p.status === "ok" && p.kind === "chat");
    const c = await api.post<Conversation>("/api/desktop/conversations", {
      title: `Agent · ${ws.name}`, mode: "agent", workspace_id: ws.id, preset: preset?.id || null, permission_mode: "ask" });
    await shared.reloadConversations();
    go({ page: "chat", id: c.id });
  };
  const crumbs = path === "." ? [] : path.split("/");

  return (
    <div className="p-5">
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold">{ws.name}</h2>
        <span className="truncate text-xs text-muted">{ws.path}</span>
        <div className="ml-auto flex gap-2">
          <button className="btn-primary py-1" onClick={newAgent}><Code2 size={14} /> Agent mới trong Project</button>
          <button className="btn py-1" title="Bỏ Project khỏi danh sách (không xóa file)" onClick={async () => {
            if (!confirm("Bỏ Project khỏi SHAMS? File trên đĩa không bị xóa.")) return;
            await api.del(`/api/desktop/workspaces/${ws.id}`);
            shared.reloadWorkspaces();
          }}><Trash2 size={14} /></button>
        </div>
      </div>
      {convs.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {convs.slice(0, 8).map((c) => <button key={c.id} className="chip hover:text-fg" onClick={() => go({ page: "chat", id: c.id })}>{c.title}</button>)}
        </div>
      )}
      <div className="mt-4 flex gap-1 border-b border-line">
        {([["files", "File"], ["git", "Git"], ["env", "Environment"]] as const).map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)} className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${tab === k ? "border-accent text-fg" : "border-transparent text-muted"}`}>{l}</button>
        ))}
      </div>
      {tab === "files" && (
        <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[280px_1fr]">
          <div className="card max-h-[60vh] overflow-y-auto p-1 text-sm">
            <div className="flex flex-wrap items-center gap-0.5 px-2 py-1 text-xs text-muted">
              <button onClick={() => setPath(".")}>{ws.name}</button>
              {crumbs.map((c, i) => <span key={i} className="flex items-center"><ChevronRight size={11} />
                <button onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>{c}</button></span>)}
            </div>
            {tree.error && <div className="p-2 text-xs text-bad">{tree.error}</div>}
            {tree.data?.items.map((it) => (
              <button key={it.path} className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left hover:bg-sunk"
                onClick={async () => it.dir ? setPath(it.path) : setFile(await api.get(`/api/desktop/workspaces/${ws.id}/file?path=${encodeURIComponent(it.path)}`))}>
                {it.dir ? <Folder size={13} className="text-accent" /> : <File size={13} className="text-muted" />}
                <span className="truncate">{it.name}</span>
              </button>
            ))}
          </div>
          <div className="card min-h-[200px] overflow-hidden">
            {file ? (
              <>
                <div className="flex items-center border-b border-line px-3 py-1.5 text-xs text-muted">{file.path}
                  <button className="ml-auto" onClick={() => setFile(null)}><X size={13} /></button></div>
                <pre className="max-h-[56vh] overflow-auto p-3 font-mono text-[12px]">{file.text}</pre>
              </>
            ) : <div className="p-6 text-center text-sm text-muted">Chọn một file để xem.</div>}
          </div>
        </div>
      )}
      {tab === "git" && (
        <div className="mt-3">
          {!git.data ? <div className="text-muted">...</div> : !git.data.git ? <div className="text-muted">Thư mục này không phải Git repo.</div> : (
            <>
              <div className="mb-2 text-sm"><GitBranch size={13} className="inline" /> {git.data.branch}</div>
              <pre className="card mb-3 max-h-40 overflow-auto p-2 font-mono text-xs">{git.data.status || "(sạch)"}</pre>
              {git.data.diff ? <DiffView diff={git.data.diff} max={2000} /> : <div className="text-sm text-muted">Không có thay đổi chưa commit.</div>}
            </>
          )}
        </div>
      )}
      {tab === "env" && (
        <div className="mt-3 max-w-2xl">
          <div className="label mb-1">Biến môi trường (cho lệnh agent chạy trong Project)</div>
          {env.map(([k, v], i) => (
            <div key={i} className="mb-1 flex gap-1">
              <input className="input w-48 font-mono text-xs" value={k} placeholder="TÊN" onChange={(e) => setEnv(env.map((x, j) => j === i ? [e.target.value, x[1]] : x))} />
              <input className="input font-mono text-xs" value={v} placeholder="giá trị" onChange={(e) => setEnv(env.map((x, j) => j === i ? [x[0], e.target.value] : x))} />
              <button className="btn-ghost" onClick={() => setEnv(env.filter((_, j) => j !== i))}><X size={13} /></button>
            </div>
          ))}
          <button className="btn-ghost text-xs" onClick={() => setEnv([...env, ["", ""]])}><Plus size={13} /> Thêm biến</button>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <label className="text-sm">Shell
              <select className="input mt-1" value={shell} onChange={(e) => setShell(e.target.value)}>
                <option value="">Mặc định (cmd trên Windows)</option><option value="powershell">PowerShell</option><option value="bash">bash</option>
              </select>
            </label>
            <label className="text-sm">Python / venv
              <input className="input mt-1 font-mono text-xs" value={python} onChange={(e) => setPython(e.target.value)} placeholder=".venv\Scripts\python.exe" />
            </label>
          </div>
          <label className="mt-3 block text-sm">Ghi chú cho agent (cách build/test, quy ước code...)
            <textarea className="input mt-1 h-28" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>
          <button className="btn-primary mt-3" onClick={save}><Save size={14} /> Lưu</button> {msg && <span className="text-sm text-ok">{msg}</span>}
        </div>
      )}
    </div>
  );
}
