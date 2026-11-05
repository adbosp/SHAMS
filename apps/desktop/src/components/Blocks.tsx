import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle, Brain, Check, ChevronDown, ChevronRight, Copy, Download, ExternalLink, FileText, Globe, Loader2,
  ShieldAlert, Terminal, Wrench, X,
} from "lucide-react";
import { api, fileUrl } from "../lib/api";
import type { Block } from "../lib/types";

// --------------------------------------------------------------------------- markdown + code
function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="my-2 overflow-hidden rounded-lg border border-line bg-sunk">
      <div className="flex items-center justify-between border-b border-line px-3 py-1 text-[11px] text-muted">
        <span>{lang || "code"}</span>
        <button className="flex items-center gap-1 hover:text-fg" onClick={() => {
          navigator.clipboard?.writeText(code);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }}>{copied ? <Check size={12} /> : <Copy size={12} />} {copied ? "Đã chép" : "Chép"}</button>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[12.5px] leading-relaxed"><code>{code}</code></pre>
    </div>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-shams">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
        code({ className, children, ...props }) {
          const m = /language-(\w+)/.exec(className || "");
          const s = String(children);
          if (m || s.includes("\n")) return <CodeBlock lang={m?.[1] || ""} code={s.replace(/\n$/, "")} />;
          return <code className={className} {...props}>{children}</code>;
        },
        pre({ children }) {
          return <>{children}</>;
        },
        a({ href, children }) {
          return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
        },
        img({ src, alt }) {
          return <img src={src} alt={alt || ""} className="my-2 max-h-96 rounded-lg border border-line" />;
        },
      }}>{text}</ReactMarkdown>
    </div>
  );
}

// --------------------------------------------------------------------------- diff
export function DiffView({ diff, max = 400 }: { diff: string; max?: number }) {
  const lines = diff.split("\n");
  return (
    <pre className="max-h-96 overflow-auto rounded-md border border-line bg-sunk p-2 font-mono text-[12px] leading-snug">
      {lines.slice(0, max).map((l, i) => (
        <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "diff-add" : l.startsWith("-") && !l.startsWith("---")
          ? "diff-del" : l.startsWith("@@") ? "diff-hunk" : ""}>{l || " "}</div>
      ))}
      {lines.length > max && <div className="text-muted">... {lines.length - max} dòng nữa</div>}
    </pre>
  );
}

// --------------------------------------------------------------------------- tools
const TOOL_LABEL: Record<string, string> = {
  list_dir: "Liệt kê thư mục", read_file: "Đọc file", search_files: "Tìm trong code", write_file: "Ghi file",
  edit_file: "Sửa file", delete_file: "Xóa file", run_command: "Chạy lệnh",
};

function toolTitle(b: Block) {
  const a = b.arguments || {};
  const target = a.command || a.path || a.pattern || "";
  return `${TOOL_LABEL[b.name] || b.name}${target ? ": " + String(target).slice(0, 90) : ""}`;
}

function StatusIcon({ s }: { s: string }) {
  if (s === "running" || s === "pending") return <Loader2 size={14} className="animate-spin text-accent" />;
  if (s === "approval") return <ShieldAlert size={14} className="text-warn" />;
  if (s === "done") return <Check size={14} className="text-ok" />;
  if (s === "denied") return <X size={14} className="text-muted" />;
  return <AlertTriangle size={14} className="text-bad" />;
}

function Approval({ b, onDecided }: { b: Block; onDecided?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const decide = async (approve: boolean) => {
    setBusy(true);
    try {
      await api.post(`/api/desktop/approvals/${b.approval_id}`, { approve, note });
      onDecided?.();
    } finally {
      setBusy(false);
    }
  };
  const p = b.preview || {};
  return (
    <div className="mt-2 rounded-md border border-warn/40 bg-warn/5 p-2.5">
      <div className="mb-1.5 flex items-center gap-1.5 text-[13px] font-medium"><ShieldAlert size={14} className="text-warn" />
        Cần bạn duyệt: {b.reason}</div>
      {p.command && <pre className="mb-2 overflow-x-auto rounded bg-sunk p-2 font-mono text-xs">$ {p.command}
        {"\n"}# trong {p.cwd}{p.class ? ` · loại: ${p.class}` : ""}</pre>}
      {p.diff && <DiffView diff={p.diff} />}
      {p.error && <div className="text-xs text-bad">{p.error}</div>}
      <div className="mt-2 flex items-center gap-2">
        <input className="input max-w-xs py-1 text-xs" placeholder="Ghi chú (tùy chọn)" value={note}
          onChange={(e) => setNote(e.target.value)} />
        <button className="btn-primary py-1" disabled={busy} onClick={() => decide(true)}><Check size={14} /> Cho phép</button>
        <button className="btn py-1" disabled={busy} onClick={() => decide(false)}><X size={14} /> Từ chối</button>
      </div>
    </div>
  );
}

function ToolItem({ b }: { b: Block }) {
  const [open, setOpen] = useState(b.status === "approval");
  const Icon = b.name === "run_command" ? Terminal : b.name?.includes("file") ? FileText : Wrench;
  return (
    <div className="border-b border-line last:border-0">
      <button className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] hover:bg-sunk" onClick={() => setOpen(!open)}>
        <StatusIcon s={b.status} />
        <Icon size={13} className="text-muted" />
        <span className="truncate">{toolTitle(b)}</span>
        {b.exit_code != null && <span className={`chip ml-1 ${b.exit_code === 0 ? "text-ok" : "text-bad"}`}>exit {b.exit_code}</span>}
        <span className="ml-auto text-muted">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
      </button>
      {(open || b.status === "approval") && (
        <div className="px-3 pb-2">
          {b.status === "approval" && <Approval b={b} />}
          {b.status !== "approval" && b.diff && <DiffView diff={b.diff} />}
          {b.status !== "approval" && b.result && !b.diff && (
            <pre className="max-h-72 overflow-auto rounded-md bg-sunk p-2 font-mono text-[12px]">{b.result}</pre>
          )}
          {b.status === "denied" && <div className="text-xs text-muted">Bị từ chối{b.note ? `: ${b.note}` : ""} ({b.reason})</div>}
        </div>
      )}
    </div>
  );
}

function ToolGroup({ items }: { items: Block[] }) {
  const busy = items.some((b) => ["running", "pending", "approval"].includes(b.status));
  const [open, setOpen] = useState(false);
  const show = open || busy;
  const failed = items.filter((b) => b.status === "error").length;
  return (
    <div className="my-2 overflow-hidden rounded-lg border border-line bg-panel">
      <button className="flex w-full items-center gap-2 px-3 py-2 text-[13px] text-muted hover:text-fg" onClick={() => setOpen(!open)}>
        {show ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Wrench size={14} /> {busy ? "Đang dùng công cụ" : "Đã dùng"} {items.length} công cụ
        {failed > 0 && <span className="text-bad">· {failed} lỗi</span>}
      </button>
      {show && <div className="border-t border-line">{items.map((b, i) => <ToolItem key={b.id || i} b={b} />)}</div>}
    </div>
  );
}

// --------------------------------------------------------------------------- media + links
function MediaGrid({ items }: { items: Block[] }) {
  const [zoom, setZoom] = useState<Block | null>(null);
  return (
    <>
      <div className={`my-2 grid gap-2 ${items.length > 1 ? "grid-cols-2 lg:grid-cols-3" : "grid-cols-1"}`}>
        {items.map((b, i) => (
          <div key={i} className="group relative overflow-hidden rounded-lg border border-line bg-sunk">
            {b.type === "image" && <img src={fileUrl(b.url)} alt={b.name} onClick={() => setZoom(b)}
              className="max-h-[480px] w-full cursor-zoom-in object-contain" />}
            {b.type === "video" && (b.mime === "image/webp"
              ? <img src={fileUrl(b.url)} alt={b.name} className="w-full object-contain" />
              : <video src={fileUrl(b.url)} controls className="w-full" />)}
            {b.type === "audio" && <audio src={fileUrl(b.url)} controls className="w-full p-2" />}
            {b.type === "model3d" && (
              <div className="flex h-32 items-center justify-center gap-2 text-sm text-muted"><FileText size={16} /> {b.name || "model.glb"}</div>
            )}
            <a href={fileUrl(b.url)} download className="absolute right-2 top-2 hidden rounded bg-black/60 p-1 text-white group-hover:block"
              title="Tải về"><Download size={14} /></a>
          </div>
        ))}
      </div>
      {zoom && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6" onClick={() => setZoom(null)}>
          <img src={fileUrl(zoom.url)} className="max-h-full max-w-full rounded-lg" alt="" />
        </div>
      )}
    </>
  );
}

function LinkGroup({ items }: { items: { url: string; kind: string }[] }) {
  const media = items.filter((l) => l.kind !== "link");
  const links = items.filter((l) => l.kind === "link");
  return (
    <div className="my-2">
      {media.length > 0 && <MediaGrid items={media.map((l) => ({ type: l.kind as Block["type"], url: l.url, name: l.url }))} />}
      {links.length > 0 && (
        <div className="mt-1">
          <div className="label mb-1 flex items-center gap-1"><Globe size={12} /> Liên kết ({links.length})</div>
          <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
            {links.map((l) => {
              let host = l.url;
              try { host = new URL(l.url).host; } catch { /* keep raw */ }
              return (
                <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
                  className="flex items-center gap-2 rounded-md border border-line bg-panel px-2.5 py-1.5 text-[13px] hover:border-accent">
                  <ExternalLink size={13} className="shrink-0 text-muted" />
                  <span className="min-w-0"><span className="block truncate font-medium">{host}</span>
                    <span className="block truncate text-[11px] text-muted">{l.url}</span></span>
                </a>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function Thinking({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="my-1.5">
      <button className="flex items-center gap-1.5 text-[12px] text-muted hover:text-fg" onClick={() => setOpen(!open)}>
        <Brain size={13} /> {live ? "Đang suy nghĩ..." : "Quá trình suy nghĩ"} {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
      </button>
      {open && <div className="mt-1 whitespace-pre-wrap border-l-2 border-line pl-3 text-[12.5px] text-muted">{text}</div>}
    </div>
  );
}

// --------------------------------------------------------------------------- grouping: text, then groups
const MEDIA = new Set(["image", "video", "model3d", "audio"]);

export function Blocks({ blocks, live }: { blocks: Block[]; live: boolean }) {
  const out: ReactNode[] = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    if (b.type === "tool") {
      const g: Block[] = [];
      const blank = (x: Block) => x.type === "text" && !(x.text || "").trim();
      while (i < blocks.length && (blocks[i].type === "tool" || blank(blocks[i]))) {
        if (blocks[i].type === "tool") g.push(blocks[i]);
        i++;
      }
      out.push(<ToolGroup key={`t${i}`} items={g} />);
      continue;
    }
    if (MEDIA.has(b.type)) {
      const g: Block[] = [];
      while (i < blocks.length && MEDIA.has(blocks[i].type)) g.push(blocks[i++]);
      out.push(<MediaGrid key={`m${i}`} items={g} />);
      continue;
    }
    if (b.type === "text" && b.text) out.push(<Markdown key={i} text={b.text} />);
    else if (b.type === "thinking" && b.text) out.push(<Thinking key={i} text={b.text} live={live && i === blocks.length - 1} />);
    else if (b.type === "links") out.push(<LinkGroup key={i} items={b.items} />);
    else if (b.type === "error") out.push(
      <div key={i} className="my-2 flex gap-2 rounded-md border border-bad/40 bg-bad/5 p-2.5 text-[13px] text-bad">
        <AlertTriangle size={15} className="mt-0.5 shrink-0" /> <span className="whitespace-pre-wrap">{b.text}</span></div>);
    else if (b.type === "notice") out.push(<div key={i} className="my-1 text-[12px] text-muted">{b.text}</div>);
    else if (b.type === "task") out.push(<div key={i} className="chip my-1">Tác vụ {b.task_id} · {b.state}</div>);
    else if (b.type === "file") out.push(
      <a key={i} href={fileUrl(b.url)} download className="chip my-1"><Download size={12} /> {b.name}</a>);
    i++;
  }
  return <>{out}</>;
}

export function Attachments({ blocks }: { blocks: Block[] }) {
  const att = blocks.filter((b) => b.type === "attachment");
  if (!att.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap justify-end gap-1.5">
      {att.map((a) => (a.mime || "").startsWith("image/")
        ? <img key={a.id} src={fileUrl(a.url)} className="h-20 rounded-md border border-line object-cover" alt={a.name} />
        : <span key={a.id} className="chip bg-panel"><FileText size={12} /> {a.name}</span>)}
    </div>
  );
}
