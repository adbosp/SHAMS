import { useState } from "react";
import { CheckCircle2, CircleSlash, Download, HelpCircle, Loader2, Play, Plus, Power, RefreshCw, Trash2, XCircle } from "lucide-react";
import { api } from "../lib/api";
import { KIND_LABEL, useData, type Shared } from "../lib/store";

type Inv = { id: string; label: string; backend: string; kind: string; use?: string; specialty?: boolean; adult?: boolean;
  status: string; detail?: string; installed_id?: string; files?: string[]; registered: boolean; size_mb?: number;
  capabilities?: string[]; workflow_available?: boolean };
type Report = {
  ollama: { status: string; version?: string; base_url?: string; error?: string;
    models: { name: string; size_mb: number; parameters?: string; quantization?: string; capabilities: string[]; context_length?: number }[];
    loaded: { name: string; size_vram: number; expires_at?: string }[] };
  comfyui: { status: string; base_url?: string; folders: Record<string, string[]>; workflows: string[]; autostart?: boolean };
  roles: { role: string; label: string; wanted: string; model_id: string | null; status: string; how: string }[];
  presets: { id: string; label: string; kind: string; provider: string; model: string | null; status: string; how: string;
    source: string; execution_modes: string[]; autonomous_default: boolean; content_policy: { adult?: boolean } }[];
  preset_config: Record<string, any>;
  inventory: Inv[];
  tools: Record<string, any>;
};

const ST: Record<string, [any, string]> = {
  installed: [CheckCircle2, "text-ok"], ok: [CheckCircle2, "text-ok"], missing: [XCircle, "text-bad"],
  incomplete: [CircleSlash, "text-warn"], unknown: [HelpCircle, "text-muted"], disabled: [Power, "text-muted"],
};

function Status({ s }: { s: string }) {
  const [Icon, cls] = ST[s] || [HelpCircle, "text-warn"];
  return <span className={`inline-flex items-center gap-1 text-xs ${cls}`}><Icon size={13} /> {s}</span>;
}

export default function ModelsPage({ shared }: { shared: Shared }) {
  const { data, error, loading, reload } = useData<Report>("/api/desktop/models");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [keep, setKeep] = useState("10m");
  const [adding, setAdding] = useState(false);

  const act = async (key: string, f: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    setMsg(null);
    try {
      await f();
      if (ok) setMsg(ok);
      await reload();
      await shared.reloadPresets();
    } catch (e: any) {
      setMsg("Lỗi: " + e.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-6 py-5">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold">Model Manager</h1>
          <button className="btn ml-auto py-1 text-xs" onClick={reload}>{loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} Dò lại</button>
        </div>
        <p className="mt-1 text-sm text-muted">Chỉ hiển thị trạng thái đã kiểm tra trên máy này. Không tự tải model lớn.</p>
        {error && <div className="mt-2 text-sm text-bad">{error}</div>}
        {msg && <div className={`mt-2 text-sm ${msg.startsWith("Lỗi") ? "text-bad" : "text-ok"}`}>{msg}</div>}
        {data && (
          <>
            {/* inventory */}
            <h2 className="label mb-2 mt-5">Danh mục model SHAMS (so khớp với file thật)</h2>
            <div className="card overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-muted"><tr className="border-b border-line">
                  <th className="p-2">Model</th><th className="p-2">Loại</th><th className="p-2">Backend</th><th className="p-2">Trạng thái</th>
                  <th className="p-2">Chi tiết</th><th className="p-2"></th></tr></thead>
                <tbody>
                  {data.inventory.map((r) => (
                    <tr key={r.id} className="border-b border-line last:border-0">
                      <td className="p-2"><div className="font-medium">{r.label}</div>
                        {r.specialty && <span className="chip mt-0.5">riêng, chỉ khi bạn chọn</span>}</td>
                      <td className="p-2 text-xs">{KIND_LABEL[r.kind] || r.kind}</td>
                      <td className="p-2 text-xs">{r.backend}</td>
                      <td className="p-2"><Status s={r.status} /></td>
                      <td className="max-w-xs p-2 text-xs text-muted">
                        <div className="truncate" title={r.installed_id || r.files?.join(", ")}>{r.installed_id || r.files?.join(", ")}</div>
                        <div className="truncate" title={r.detail}>{r.detail}</div>
                        {r.workflow_available === false && <div className="text-warn">chưa có workflow</div>}
                      </td>
                      <td className="p-2 text-right">
                        {r.registered ? <span className="chip text-ok">đã đăng ký</span> : r.status === "installed" ? (
                          <button className="btn py-0.5 text-xs" disabled={!!busy}
                            onClick={() => act(r.id, () => api.post("/api/desktop/models/register", { inventory_id: r.id }), `Đã đăng ký ${r.label}`)}>
                            {busy === r.id ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} Đăng ký</button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* presets */}
            <div className="mb-2 mt-6 flex items-center">
              <h2 className="label">Preset trong registry</h2>
              <button className="btn ml-auto py-0.5 text-xs" onClick={() => setAdding(!adding)}><Plus size={12} /> Preset mới</button>
            </div>
            {adding && <AddPreset onSave={(id, cfg) => act("add", () => api.post("/api/desktop/models/presets", { id, config: cfg }), `Đã lưu ${id}`).then(() => setAdding(false))}
              ollama={data.ollama.models.map((m) => m.name)} workflows={data.comfyui.workflows} />}
            <div className="card divide-y divide-line">
              {data.presets.map((p) => {
                const user = !!data.preset_config[p.id];
                const enabled = data.preset_config[p.id]?.enabled !== false;
                return (
                  <div key={p.id} className="flex flex-wrap items-center gap-2 p-2 text-sm">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium">{p.label} <span className="text-xs text-muted">({p.id})</span></div>
                      <div className="truncate text-xs text-muted">{KIND_LABEL[p.kind] || p.kind} · {p.provider} · {p.model || "—"} ·
                        {p.autonomous_default ? " Autonomous + Direct" : " chỉ Direct"}{p.source === "role" ? " · vai trò" : ""}{p.content_policy?.adult ? " · 18+" : ""}</div>
                      {p.status !== "ok" && <div className="truncate text-xs text-warn">{p.how}</div>}
                    </div>
                    <Status s={p.status} />
                    {user && (
                      <>
                        <button className="btn py-0.5 text-xs" disabled={!!busy}
                          onClick={() => act(p.id, () => api.post(`/api/desktop/models/presets/${p.id}/enabled`, { enabled: !enabled }))}>
                          <Power size={12} /> {enabled ? "Tắt" : "Bật"}</button>
                        <button className="btn-ghost py-0.5" title="Xóa khỏi registry (không xóa file model)" disabled={!!busy}
                          onClick={() => confirm(`Xóa preset ${p.id}? File model không bị xóa.`) && act(p.id, () => api.del(`/api/desktop/models/presets/${p.id}`))}>
                          <Trash2 size={13} /></button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>

            {/* ollama */}
            <div className="mb-2 mt-6 flex items-center gap-2">
              <h2 className="label">Ollama · {data.ollama.status}{data.ollama.version ? ` · v${data.ollama.version}` : ""}</h2>
              <span className="ml-auto text-xs text-muted">keep_alive</span>
              <input className="input w-20 py-0.5 text-xs" value={keep} onChange={(e) => setKeep(e.target.value)} />
              <button className="btn py-0.5 text-xs" disabled={!!busy} onClick={() => act("unall", () => api.post("/api/desktop/models/ollama/unload", {}), "Đã dỡ mọi model")}>Dỡ tất cả</button>
            </div>
            {data.ollama.error && <div className="mb-2 text-xs text-bad">{data.ollama.error}</div>}
            <div className="card divide-y divide-line">
              {data.ollama.models.map((m) => {
                const loaded = data.ollama.loaded.find((l) => l.name === m.name);
                return (
                  <div key={m.name} className="flex items-center gap-2 p-2 text-sm">
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-[13px]">{m.name}</div>
                      <div className="text-xs text-muted">{(m.size_mb / 1024).toFixed(1)} GB · {m.parameters || "?"} · {m.quantization || "?"} ·
                        {" "}{m.capabilities.join(", ") || "?"}{m.context_length ? ` · ctx ${m.context_length}` : ""}</div>
                    </div>
                    {loaded ? <span className="chip text-ok">đang nạp {(loaded.size_vram / 2 ** 30).toFixed(1)} GB VRAM</span> : null}
                    {loaded ? (
                      <button className="btn py-0.5 text-xs" disabled={!!busy} onClick={() => act(m.name, () => api.post("/api/desktop/models/ollama/unload", { model: m.name }))}>Dỡ</button>
                    ) : (
                      <button className="btn py-0.5 text-xs" disabled={!!busy} onClick={() => act(m.name, () => api.post("/api/desktop/models/ollama/load", { model: m.name, keep_alive: keep }))}>
                        {busy === m.name ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />} Nạp</button>
                    )}
                  </div>
                );
              })}
            </div>

            {/* comfyui */}
            <div className="mb-2 mt-6 flex items-center gap-2">
              <h2 className="label">ComfyUI · {data.comfyui.status}{data.comfyui.base_url ? ` · ${data.comfyui.base_url}` : ""}</h2>
              {data.comfyui.status === "down" && data.comfyui.autostart && (
                <button className="btn ml-auto py-0.5 text-xs" disabled={!!busy} onClick={() => act("cstart", () => api.post("/api/desktop/models/comfyui/start"), "ComfyUI đã chạy")}>
                  <Play size={12} /> Khởi động</button>
              )}
              {data.comfyui.status === "ok" && (
                <button className="btn ml-auto py-0.5 text-xs" disabled={!!busy} onClick={() => act("cfree", () => api.post("/api/desktop/models/comfyui/free"), "Đã giải phóng VRAM ComfyUI")}>Giải phóng VRAM</button>
              )}
            </div>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
              {Object.entries(data.comfyui.folders).map(([f, files]) => (
                <div key={f} className="card p-2">
                  <div className="label mb-1">{f} ({files.length})</div>
                  <div className="max-h-32 overflow-y-auto font-mono text-[11px] text-muted">{files.map((x) => <div key={x} className="truncate">{x}</div>)}</div>
                </div>
              ))}
              <div className="card p-2">
                <div className="label mb-1">workflows ({data.comfyui.workflows.length})</div>
                <div className="font-mono text-[11px] text-muted">{data.comfyui.workflows.map((x) => <div key={x}>{x}</div>)}</div>
              </div>
            </div>

            {/* roles */}
            <h2 className="label mb-2 mt-6">Vai trò trong Autonomous Mode</h2>
            <div className="card divide-y divide-line">
              {data.roles.map((r) => (
                <div key={r.role} className="flex items-center gap-2 p-2 text-sm">
                  <span className="w-48 font-medium">{r.label}</span>
                  <span className="flex-1 truncate font-mono text-xs text-muted">{r.model_id || r.wanted || "—"} {r.how ? `(${r.how})` : ""}</span>
                  <Status s={r.status} />
                </div>
              ))}
            </div>
            <div className="mt-2 flex items-center gap-1 text-xs text-muted"><Download size={12} /> Đổi model cho vai trò trong Settings (models.roles).</div>
          </>
        )}
      </div>
    </div>
  );
}

function AddPreset({ onSave, ollama, workflows }: { onSave: (id: string, cfg: any) => void; ollama: string[]; workflows: string[] }) {
  const [id, setId] = useState("");
  const [label, setLabel] = useState("");
  const [provider, setProvider] = useState("ollama");
  const [model, setModel] = useState(ollama[0] || "");
  const [workflow, setWorkflow] = useState(workflows[0] || "");
  const [kind, setKind] = useState("chat");
  const [params, setParams] = useState("{}");
  const [auto, setAuto] = useState(false);
  return (
    <div className="card mb-3 grid grid-cols-2 gap-2 p-3 text-sm md:grid-cols-4">
      <input className="input" placeholder="id (vd. noobai)" value={id} onChange={(e) => setId(e.target.value)} />
      <input className="input" placeholder="Tên hiển thị" value={label} onChange={(e) => setLabel(e.target.value)} />
      <select className="input" value={provider} onChange={(e) => {
        setProvider(e.target.value);
        setKind(e.target.value === "comfyui" ? "image" : "chat");
      }}><option value="ollama">ollama</option><option value="comfyui">comfyui</option></select>
      <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
        {["chat", "image", "video", "scene3d"].map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
      </select>
      {provider === "ollama" ? (
        <select className="input col-span-2" value={model} onChange={(e) => setModel(e.target.value)}>{ollama.map((m) => <option key={m}>{m}</option>)}</select>
      ) : (
        <select className="input col-span-2" value={workflow} onChange={(e) => setWorkflow(e.target.value)}>{workflows.map((m) => <option key={m}>{m}</option>)}</select>
      )}
      <input className="input col-span-2 font-mono text-xs" value={params} onChange={(e) => setParams(e.target.value)} placeholder='params JSON, vd. {"checkpoint": "x.safetensors"}' />
      <label className="col-span-2 flex items-center gap-2 text-xs text-muted">
        <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> Cho Autonomous Mode tự dùng (mặc định tắt)</label>
      <button className="btn-primary col-span-2 justify-center" disabled={!id} onClick={() => {
        let p = {};
        try { p = JSON.parse(params || "{}"); } catch { alert("params không phải JSON hợp lệ"); return; }
        onSave(id, { label: label || id, provider, kind, ...(provider === "ollama" ? { model } : { workflow }),
          params: p, execution_modes: auto ? ["direct", "autonomous"] : ["direct"], autonomous_default: auto });
      }}>Lưu preset</button>
    </div>
  );
}
