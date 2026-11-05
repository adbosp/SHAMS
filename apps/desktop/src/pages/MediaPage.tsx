import { useState } from "react";
import { Box, Download, ExternalLink, Film } from "lucide-react";
import { fileUrl } from "../lib/api";
import { timeAgo, useData } from "../lib/store";
import type { Route } from "../lib/types";

type Item = { type: string; url: string; name: string; mime: string; preset: string; conversation_id: string; created: number };

export default function MediaPage({ go }: { go: (r: Route) => void }) {
  const { data, reload } = useData<{ items: Item[] }>("/api/desktop/media");
  const [kind, setKind] = useState("all");
  const [zoom, setZoom] = useState<Item | null>(null);
  const items = (data?.items || []).filter((i) => kind === "all" || i.type === kind);
  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 py-5">
        <div className="mb-4 flex items-center gap-2">
          <h1 className="text-xl font-semibold">Media Gallery</h1>
          <div className="ml-4 flex overflow-hidden rounded-md border border-line text-xs">
            {[["all", "Tất cả"], ["image", "Ảnh"], ["video", "Video"], ["model3d", "3D"]].map(([k, l]) => (
              <button key={k} onClick={() => setKind(k)} className={`px-2.5 py-1 ${kind === k ? "bg-accent text-white" : "hover:bg-sunk"}`}>{l}</button>
            ))}
          </div>
          <button className="btn ml-auto py-1 text-xs" onClick={reload}>Làm mới</button>
        </div>
        {items.length === 0 && <div className="card p-8 text-center text-muted">Chưa có ảnh/video/3D. Tạo bằng Direct AI với preset ảnh hoặc video.</div>}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          {items.map((it, i) => (
            <div key={i} className="group card overflow-hidden">
              <div className="flex aspect-square items-center justify-center bg-sunk">
                {it.type === "image" || it.mime === "image/webp" ? (
                  <img src={fileUrl(it.url)} className="h-full w-full cursor-zoom-in object-cover" onClick={() => setZoom(it)} alt="" />
                ) : it.type === "video" ? <video src={fileUrl(it.url)} className="h-full w-full object-cover" muted controls />
                  : it.type === "model3d" ? <Box size={32} className="text-muted" /> : <Film size={32} className="text-muted" />}
              </div>
              <div className="flex items-center gap-1 p-2 text-[11px] text-muted">
                <span className="truncate">{it.preset} · {timeAgo(it.created)}</span>
                <button className="ml-auto hover:text-fg" title="Mở cuộc trò chuyện" onClick={() => go({ page: "chat", id: it.conversation_id })}><ExternalLink size={12} /></button>
                <a className="hover:text-fg" href={fileUrl(it.url)} download title="Xuất file"><Download size={12} /></a>
              </div>
            </div>
          ))}
        </div>
      </div>
      {zoom && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6" onClick={() => setZoom(null)}>
          <img src={fileUrl(zoom.url)} className="max-h-full max-w-full rounded-lg" alt="" />
        </div>
      )}
    </div>
  );
}
