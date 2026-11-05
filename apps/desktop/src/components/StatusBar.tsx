import { Activity, Cpu, HardDrive, Lock } from "lucide-react";
import { useData } from "../lib/store";

type Res = {
  hardware: { vram: { name: string; used_mb: number | null; total_mb: number; utilization_pct: number | null } | null;
    ram?: { total_mb?: number | null; available_mb?: number | null } };
  gpu_gate: { serialize: boolean; holder: { owner: string; model: string | null; kind: string } | null; waiting: unknown[] };
  ollama_loaded?: { name: string; size_vram: number }[];
  jobs: unknown[];
};

const gb = (mb?: number | null) => (mb == null ? "?" : (mb / 1024).toFixed(1));

export default function StatusBar({ health }: { health: { version: string; workspace: string } }) {
  const { data } = useData<Res>("/api/desktop/resources", [], 3000);
  const v = data?.hardware.vram;
  const mem = data?.hardware.ram;
  const holder = data?.gpu_gate.holder;
  return (
    <footer className="flex h-7 shrink-0 items-center gap-4 overflow-hidden border-t border-line bg-panel px-3 text-[11px] text-muted">
      <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-ok" /> Core {health.version}</span>
      {v && (
        <span className="flex items-center gap-1" title={v.name}>
          <HardDrive size={12} /> VRAM {gb(v.used_mb)}/{gb(v.total_mb)} GB{v.utilization_pct != null ? ` · GPU ${v.utilization_pct}%` : ""}
        </span>
      )}
      {mem?.total_mb && (
        <span className="flex items-center gap-1"><Cpu size={12} /> RAM {gb((mem.total_mb ?? 0) - (mem.available_mb ?? 0))}/{gb(mem.total_mb)} GB</span>
      )}
      {data?.ollama_loaded && data.ollama_loaded.length > 0 && (
        <span className="truncate">Đang nạp: {data.ollama_loaded.map((m) => m.name).join(", ")}</span>
      )}
      {holder && (
        <span className="flex items-center gap-1 text-accent"><Lock size={12} /> GPU: {holder.owner}{holder.model ? ` (${holder.model})` : ""}
          {data!.gpu_gate.waiting.length ? ` · ${data!.gpu_gate.waiting.length} chờ` : ""}</span>
      )}
      {data && data.jobs.length > 0 && <span className="flex items-center gap-1"><Activity size={12} /> {data.jobs.length} job</span>}
      <span className="ml-auto truncate" title={health.workspace}>{health.workspace}</span>
    </footer>
  );
}
