import { useState } from "react";
import { Box, Brain, Code2, Film, Image as ImageIcon, MessageSquare, Shield } from "lucide-react";
import { KIND_LABEL, type Shared } from "../lib/store";
import type { Preset, Route } from "../lib/types";
import ChatView from "./ChatView";

const KIND_ICON: Record<string, any> = { chat: MessageSquare, image: ImageIcon, video: Film, scene3d: Box, image_to_3d: Box };

/** Direct AI: pick one registered model/preset and use it directly — no planner, no DAG. */
export default function DirectPage({ shared, go }: { shared: Shared; go: (r: Route) => void }) {
  const [picked, setPicked] = useState<string | null>(null);
  const direct = shared.presets.filter((p) => p.execution_modes.includes("direct"));
  if (picked) {
    return (
      <div className="flex h-full flex-col">
        <div className="flex items-center gap-2 border-b border-line px-4 py-1.5 text-xs text-muted">
          <button className="btn-ghost py-0.5 text-xs" onClick={() => setPicked(null)}>← Tất cả model</button>
          Direct AI: một model, không Planner, không DAG.
        </div>
        <div className="min-h-0 flex-1"><ChatView key={picked} shared={shared} go={go} initialPreset={picked} /></div>
      </div>
    );
  }
  const groups: Record<string, Preset[]> = {};
  for (const p of direct) (groups[p.kind] ||= []).push(p);
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-6 py-6">
        <h1 className="text-xl font-semibold">Direct AI</h1>
        <p className="mb-5 mt-1 text-sm text-muted">
          Chọn một model hoặc preset đã đăng ký để chat, suy luận, viết code (chế độ Agent với Project), phân tích file,
          tạo ảnh, video hoặc 3D — chạy thẳng, không kích hoạt Planner. Vẫn dùng chung GPU Scheduler, Artifact Registry và QC.
        </p>
        {direct.length === 0 && <div className="card p-6 text-center text-muted">Chưa có preset nào. Mở Model Manager để đăng ký model đã cài.</div>}
        {Object.entries(groups).map(([kind, ps]) => {
          const Icon = KIND_ICON[kind] || Brain;
          return (
            <section key={kind} className="mb-6">
              <div className="label mb-2 flex items-center gap-1.5"><Icon size={13} /> {KIND_LABEL[kind] || kind}</div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
                {ps.map((p) => (
                  <button key={p.id} disabled={p.status !== "ok"} onClick={() => setPicked(p.id)}
                    className="card p-3 text-left transition hover:border-accent disabled:cursor-not-allowed disabled:opacity-60">
                    <div className="flex items-center gap-2">
                      <Icon size={16} className="text-accent" />
                      <span className="truncate font-medium">{p.label}</span>
                      {p.content_policy?.adult && <span className="chip text-warn"><Shield size={11} /> 18+</span>}
                    </div>
                    <div className="mt-1 truncate text-xs text-muted">{p.model || p.wanted || p.provider}</div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      <span className={`chip ${p.status === "ok" ? "text-ok" : "text-warn"}`}>{p.status}</span>
                      <span className="chip">{p.provider}</span>
                      {p.resources?.vram_mb ? <span className="chip">~{(p.resources.vram_mb / 1024).toFixed(1)} GB VRAM</span> : null}
                      {p.source === "role" && <span className="chip"><Code2 size={10} /> vai trò</span>}
                      {!p.autonomous_default && p.source === "preset" && <span className="chip">chỉ direct</span>}
                    </div>
                    {p.status !== "ok" && p.how && <div className="mt-1 line-clamp-2 text-[11px] text-muted">{p.how}</div>}
                  </button>
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
