import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, ArrowUp, CheckCircle2, Cpu, FolderOpen, Gamepad2, Layers, Play, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { api, subscribe } from "../lib/api";

type Task = { task_id: string; title: string; state: string; label: string; attempt: number; capability: string;
  depends_on: string[]; worker: string | null };
type State = {
  project: { id: string | null; name: string; setup: string; dir: string | null };
  projects: { id: string; folder: string; goal: string; mock: boolean }[];
  mode: string; mock_mode: boolean; running: boolean;
  progress: { done: number; total: number };
  chat: { role: string; text: string; ts: string; url?: string }[];
  tasks: Task[];
  active: { label: string; lane: string; task_id: string; model: string | null; gpu: boolean } | null;
  next_worker: string | null;
  models: { lane: string; worker_id: string; model: string; loaded_at: string }[];
  artifacts: { logical_id: string; version: number; state: string; media_type: string }[];
  qc: { task_id: string; attempt: number; gate: string; verdict: string; stopped_at_stage: number | null }[];
  console: { ts: string; text: string; type: string }[];
  review: Task[];
  vram: { name: string; used_mb: number | null; total_mb: number } | null;
  gpu_gate?: { holder: { owner: string; model: string | null } | null; waiting: unknown[] };
  playable?: { task_id: string; url: string | null; dir: string; attempt: number; failed: string[] }[];
  error?: string;
};

const STATE_COLOR: Record<string, string> = {
  ACCEPTED: "border-ok/50 bg-ok/10", RUNNING: "border-accent bg-accent/10", LEASED: "border-accent/60 bg-accent/5",
  QC_PENDING: "border-accent/60 bg-accent/5", READY: "border-line", PENDING: "border-line opacity-70",
  REPAIR_REQUIRED: "border-warn bg-warn/10", HUMAN_REVIEW: "border-warn bg-warn/10", BLOCKED: "border-bad/60 bg-bad/10",
  FAILED: "border-bad bg-bad/10", STALE: "border-warn/60",
};

function depthOf(tasks: Task[]): Record<string, number> {
  const by = Object.fromEntries(tasks.map((t) => [t.task_id, t]));
  const memo: Record<string, number> = {};
  const d = (id: string, seen = new Set<string>()): number => {
    if (memo[id] != null) return memo[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const t = by[id];
    const v = t && t.depends_on.length ? 1 + Math.max(...t.depends_on.map((x) => d(x, seen))) : 0;
    return (memo[id] = v);
  };
  tasks.forEach((t) => d(t.task_id));
  return memo;
}

export default function AutonomousPage() {
  const [st, setSt] = useState<State | null>(null);
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => subscribe("/api/desktop/autonomous/stream", "state", setSt), []);

  const cols = useMemo(() => {
    if (!st) return [];
    const depth = depthOf(st.tasks);
    const out: Task[][] = [];
    for (const t of st.tasks) (out[depth[t.task_id] || 0] ||= []).push(t);
    return out.filter(Boolean);
  }, [st]);

  const act = async (f: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await f();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!st) return <div className="p-6 text-muted">Đang kết nối luồng tiến độ...</div>;
  const transitions = st.console.filter((c) => c.type.startsWith("model."));
  const errors = st.console.filter((c) => /qc\.failed|repair|blocked|hook\.failed|not_verified|exhausted/.test(c.type));
  const pct = st.progress.total ? Math.round((100 * st.progress.done) / st.progress.total) : 0;

  return (
    <div className="flex h-full">
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="px-6 py-5">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-xl font-semibold">Autonomous Workflow</h1>
            <select className="input ml-auto w-auto py-1 text-xs" value={st.project.id || ""} onChange={(e) =>
              act(() => e.target.value ? api.post("/api/projects/open", { id: e.target.value }) : api.post("/api/projects/close"))}>
              <option value="">— Dự án mới —</option>
              {st.projects.map((p) => <option key={p.id} value={p.id}>{p.goal || p.folder}{p.mock ? " (thử)" : ""}</option>)}
            </select>
            <button className="btn py-1" disabled={busy} onClick={() => act(() => api.post("/api/run"))}><Play size={14} /> Tiếp tục</button>
            {st.project.id && (
              <button className="btn py-1" disabled={busy} title="Xóa vĩnh viễn thư mục dự án" onClick={() => {
                if (!confirm(`Xóa VĨNH VIỄN dự án "${st.project.name}"? Toàn bộ thư mục dự án bị xóa khỏi ổ đĩa, không khôi phục được (có ghi vào _logs/deletions.log).`)) return;
                act(() => api.post("/api/projects/delete", { id: st.project.id }));
              }}><Trash2 size={14} /></button>
            )}
          </div>
          <div className="mt-1 text-sm text-muted">
            {st.project.name} · {st.project.setup} · chế độ {st.mode}{st.mock_mode ? " · chạy thử (không gọi model)" : ""}
            {st.running ? " · đang chạy" : ""}
          </div>
          {err && <div className="mt-2 text-sm text-bad">{err}</div>}

          {/* goal */}
          <div className="card mt-4 flex items-end gap-2 p-2">
            <textarea className="min-h-[44px] flex-1 resize-none bg-transparent px-2 py-1 outline-none"
              placeholder={st.tasks.length ? "Ghi chú thêm cho dự án..." : "Mô tả mục tiêu. SHAMS lập DAG, chọn worker, chạy tuần tự trên GPU, QC từng bước."}
              value={goal} onChange={(e) => setGoal(e.target.value)} />
            <button className="btn-primary" disabled={busy || !goal.trim()}
              onClick={() => act(async () => {
                await api.post("/api/chat", { text: goal });
                setGoal("");
              })}><ArrowUp size={16} /></button>
          </div>

          {/* playable builds */}
          {st.playable && st.playable.length > 0 && (
            <div className="mt-4 flex flex-col gap-2">
              {st.playable.map((g) => (
                <div key={g.task_id} className={`card flex items-center gap-3 p-3 ${g.failed.length ? "border-warn/50" : "border-ok/50"}`}>
                  <Gamepad2 size={20} className={g.failed.length ? "text-warn" : "text-ok"} />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">{g.failed.length ? "Bản build chưa qua hết kiểm tra" : "Bản build đã qua kiểm tra"} · {g.task_id}</div>
                    <div className="truncate text-xs text-muted">{g.failed.length ? `Chưa đạt: ${g.failed.join(", ")} · ` : ""}{g.dir}</div>
                  </div>
                  {g.url && <button className="btn-primary" onClick={() => act(() => api.post("/api/desktop/open-url", { url: g.url }))}>
                    <Play size={14} /> Chơi thử</button>}
                </div>
              ))}
            </div>
          )}

          {/* progress */}
          <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat icon={CheckCircle2} label="Tiến độ" value={`${st.progress.done}/${st.progress.total}`} sub={`${pct}%`} />
            <Stat icon={Activity} label="Worker đang chạy" value={st.active?.label || "—"} sub={st.active ? `${st.active.task_id} · ${st.active.lane}` : st.next_worker ? `tiếp: ${st.next_worker}` : ""} />
            <Stat icon={Layers} label="Model đang nạp" value={st.models[0]?.model || st.active?.model || "—"} sub={st.models[0]?.lane || ""} />
            <Stat icon={Cpu} label="VRAM" value={st.vram ? `${((st.vram.used_mb ?? 0) / 1024).toFixed(1)}/${(st.vram.total_mb / 1024).toFixed(1)} GB` : "—"}
              sub={st.gpu_gate?.holder ? `GPU: ${st.gpu_gate.holder.owner}` : "GPU rảnh"} />
          </div>
          <div className="mt-2 h-1.5 overflow-hidden rounded bg-sunk"><div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} /></div>

          {/* DAG */}
          <h2 className="label mb-2 mt-5">Task DAG</h2>
          {st.tasks.length === 0 ? <div className="card p-6 text-center text-muted">Chưa có kế hoạch. Nhập mục tiêu ở trên.</div> : (
            <div className="flex gap-3 overflow-x-auto pb-2">
              {cols.map((col, i) => (
                <div key={i} className="flex w-56 shrink-0 flex-col gap-2">
                  <div className="text-[11px] text-muted">Bước {i + 1}</div>
                  {col.map((t) => (
                    <div key={t.task_id} className={`rounded-md border p-2 text-xs ${STATE_COLOR[t.state] || "border-line"}`}>
                      <div className="flex items-center justify-between gap-1">
                        <span className="truncate font-medium">{t.title}</span>
                        <span className="shrink-0 font-mono text-[10px]">{t.label}</span>
                      </div>
                      <div className="mt-0.5 truncate text-muted">{t.capability}{t.worker ? ` · ${t.worker}` : " · chưa có worker"}</div>
                      {t.attempt > 1 && <div className="text-warn">lần thử {t.attempt}</div>}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}

          {/* review */}
          {st.review.length > 0 && (
            <>
              <h2 className="label mb-2 mt-5 flex items-center gap-1"><AlertTriangle size={12} /> Cần bạn quyết định</h2>
              <div className="card divide-y divide-line">
                {st.review.map((t) => (
                  <div key={t.task_id} className="flex items-center gap-2 p-2 text-sm">
                    <span className="flex-1 truncate">{t.title} <span className="text-muted">({t.state})</span></span>
                    {(t.state === "BLOCKED" ? ["unblock", "cancel"] : ["accept", "retry", "fail", "cancel"]).map((d) => (
                      <button key={d} className="btn py-0.5 text-xs" disabled={busy}
                        onClick={() => act(() => api.post("/api/decide", { task_id: t.task_id, decision: d }))}>{d}</button>
                    ))}
                  </div>
                ))}
              </div>
            </>
          )}

          <div className="mt-5 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Panel title="Chuyển model (GPU)" items={transitions.map((c) => `${c.ts}  ${c.text}`)} empty="Chưa có lần nạp/dỡ model." />
            <Panel title="Lỗi và phục hồi" items={errors.map((c) => `${c.ts}  ${c.text}`)} empty="Không có lỗi." />
            <Panel title="Kết quả QC" items={st.qc.map((q) => `${q.task_id} #${q.attempt} ${q.gate}: ${q.verdict}${q.stopped_at_stage ? ` (stage ${q.stopped_at_stage})` : ""}`)} empty="Chưa có QC." icon={ShieldCheck} />
            <Panel title="Artifacts" items={st.artifacts.map((a) => `${a.logical_id} v${a.version} · ${a.media_type} · ${a.state}`)} empty="Chưa có artifact." icon={FolderOpen} />
          </div>
        </div>
      </div>
      {/* console */}
      <aside className="hidden w-80 shrink-0 flex-col border-l border-line bg-panel xl:flex">
        <div className="flex items-center gap-1.5 border-b border-line px-3 py-2 text-xs font-medium text-muted"><RefreshCw size={12} /> Nhật ký sự kiện</div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2 font-mono text-[11px] leading-relaxed">
          {st.console.slice().reverse().map((c, i) => <div key={i}><span className="text-muted">{c.ts}</span> {c.text}</div>)}
        </div>
        <div className="max-h-60 overflow-y-auto border-t border-line p-2 text-xs">
          {st.chat.slice(-6).map((m, i) => (
            <div key={i} className="mb-1.5"><span className="font-medium">{m.role === "user" ? "Bạn" : "Director"}:</span> {m.text}
              {m.url && <button className="ml-1 text-accent underline" onClick={() => api.post("/api/desktop/open-url", { url: m.url })}>mở</button>}</div>
          ))}
        </div>
      </aside>
    </div>
  );
}

function Stat({ icon: Icon, label, value, sub }: { icon: any; label: string; value: string; sub?: string }) {
  return (
    <div className="card p-3">
      <div className="label flex items-center gap-1"><Icon size={12} /> {label}</div>
      <div className="mt-1 truncate font-semibold">{value}</div>
      {sub && <div className="truncate text-xs text-muted">{sub}</div>}
    </div>
  );
}

function Panel({ title, items, empty, icon: Icon = Activity }: { title: string; items: string[]; empty: string; icon?: any }) {
  return (
    <div className="card">
      <div className="label flex items-center gap-1 border-b border-line px-3 py-2"><Icon size={12} /> {title}</div>
      <div className="max-h-48 overflow-y-auto p-2 font-mono text-[11.5px]">
        {items.length ? items.slice(-60).reverse().map((t, i) => <div key={i} className="truncate">{t}</div>) : <div className="text-muted">{empty}</div>}
      </div>
    </div>
  );
}
