import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp, Bot, Code2, Folder, Loader2, MessageSquare, Paperclip, Settings2, ShieldCheck, Square, Trash2, X,
} from "lucide-react";
import { api, applyEvent, streamJob, type ConversationDetail } from "../lib/api";
import { KIND_LABEL, usePersisted, type Shared } from "../lib/store";
import type { Conversation, Message, Preset, Route } from "../lib/types";
import { Attachments, Blocks } from "../components/Blocks";
import ParamsForm from "../components/ParamsForm";

const PERMS = [
  { id: "ask", label: "Hỏi trước mỗi hành động" },
  { id: "safe", label: "Cho phép thao tác an toàn trong Project" },
  { id: "auto", label: "Tự động, có rào chắn" },
];
const MEDIA_KINDS = new Set(["image", "video", "scene3d", "image_to_3d", "audio"]);

type Att = { id: string; name: string; mime: string; size: number; url: string };

export function PresetPicker({ presets, value, onChange, filter }: {
  presets: Preset[]; value: string; onChange: (id: string) => void; filter?: (p: Preset) => boolean;
}) {
  const list = presets.filter((p) => p.execution_modes.includes("direct") && (!filter || filter(p)));
  const groups: Record<string, Preset[]> = {};
  for (const p of list) (groups[p.kind] ||= []).push(p);
  return (
    <select className="input w-auto max-w-[260px] py-1 text-xs" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— Chọn model / preset —</option>
      {Object.entries(groups).map(([k, ps]) => (
        <optgroup key={k} label={KIND_LABEL[k] || k}>
          {ps.map((p) => (
            <option key={p.id} value={p.id} disabled={p.status !== "ok"}>
              {p.label}{p.model && p.model !== p.label ? ` · ${p.model}` : ""}{p.status !== "ok" ? ` (${p.status})` : ""}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

export default function ChatView({ id, shared, go, initialPreset }: {
  id?: string; shared: Shared; go: (r: Route) => void; initialPreset?: string;
}) {
  const [conv, setConv] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [atts, setAtts] = useState<Att[]>([]);
  const [lastPreset, setLastPreset] = usePersisted("shams.lastPreset", "");
  const [preset, setPreset] = useState(initialPreset || "");
  const [mode, setMode] = useState<"chat" | "agent">("chat");
  const [ws, setWs] = useState("");
  const [perm, setPerm] = useState("ask");
  const [params, setParams] = useState<Record<string, any>>({});
  const [showParams, setShowParams] = useState(false);
  const [confirmAdult, setConfirmAdult] = useState(false);
  const [jobs, setJobs] = useState<Record<string, string>>({});   // job id -> message id
  const [err, setErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const subs = useRef<Record<string, () => void>>({});
  const fileInput = useRef<HTMLInputElement>(null);

  const p = useMemo(() => shared.presets.find((x) => x.id === preset), [shared.presets, preset]);
  const isMedia = !!p && MEDIA_KINDS.has(p.kind);

  const follow = useCallback((jobId: string, mid: string) => {
    if (subs.current[jobId]) return;
    setJobs((j) => ({ ...j, [jobId]: mid }));
    subs.current[jobId] = streamJob(jobId, (ev) => {
      if (ev.type === "message") return;
      setMessages((ms) => ms.map((m) => (m.id === mid ? applyEvent(m, ev) : m)));
    }, () => {
      delete subs.current[jobId];
      setJobs((j) => {
        const n = { ...j };
        delete n[jobId];
        return n;
      });
      shared.reloadConversations();
    });
  }, [shared]);

  const load = useCallback(async (cid: string) => {
    const d = await api.get<ConversationDetail>(`/api/desktop/conversations/${cid}`);
    setConv(d.conversation);
    setMessages(d.messages);
    setPreset(d.conversation.preset || "");
    setMode(d.conversation.mode);
    setWs(d.conversation.workspace_id || "");
    setPerm(d.conversation.permission_mode);
    for (const m of d.messages) if (m.status === "streaming" && m.job_id) follow(m.job_id, m.id);
  }, [follow]);

  useEffect(() => {
    if (id) load(id).catch((e) => setErr(e.message));
    else if (!preset && lastPreset) setPreset(lastPreset);
    return () => {
      Object.values(subs.current).forEach((f) => f());
      subs.current = {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  const patchConv = async (fields: Partial<Conversation>) => {
    if (!conv) return;
    setConv(await api.patch<Conversation>(`/api/desktop/conversations/${conv.id}`, fields));
    shared.reloadConversations();
  };

  const addFiles = async (files: FileList | File[]) => {
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
        const a = await api.upload(f);
        setAtts((x) => [...x, a]);
      }
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setUploading(false);
    }
  };

  const send = async () => {
    if ((!text.trim() && !atts.length) || !preset) return;
    setErr(null);
    try {
      let c = conv;
      if (!c) {
        c = await api.post<Conversation>("/api/desktop/conversations", {
          preset, mode, workspace_id: ws || null, permission_mode: perm,
        });
        setConv(c);
      }
      const r = await api.post<{ job_id: string; message_id: string; user_message_id: string }>(
        `/api/desktop/conversations/${c.id}/send`,
        { text, attachments: atts, preset, mode, params, confirm_adult: confirmAdult });
      setLastPreset(preset);
      const now = Date.now() / 1000;
      const blocks = [...(text ? [{ type: "text" as const, text }] : []), ...atts.map((a) => ({ type: "attachment" as const, ...a }))];
      setMessages((ms) => [...ms,
        { id: r.user_message_id, conversation_id: c!.id, role: "user", blocks, status: "done", created: now },
        { id: r.message_id, conversation_id: c!.id, role: "assistant", blocks: [], status: "streaming", created: now, preset }]);
      setText("");
      setAtts([]);
      follow(r.job_id, r.message_id);
      if (!id) {
        shared.reloadConversations();
        go({ page: "chat", id: c.id });      // remount on the saved conversation; it re-attaches to the live stream
      }
    } catch (e: any) {
      setErr(e.message);
    }
  };

  const stop = async () => {
    for (const j of Object.keys(jobs)) await api.post(`/api/desktop/jobs/${j}/cancel`).catch(() => undefined);
  };

  const busy = Object.keys(jobs).length > 0;
  const workspace = shared.workspaces.find((w) => w.id === ws);

  return (
    <div className="flex h-full flex-col"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
      }}>
      {/* header */}
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
        {mode === "agent" ? <Code2 size={16} className="text-accent" /> : <MessageSquare size={16} className="text-muted" />}
        <input className="min-w-0 flex-1 bg-transparent font-medium outline-none" value={conv?.title ?? "Cuộc trò chuyện mới"}
          disabled={!conv} onChange={(e) => conv && setConv({ ...conv, title: e.target.value })}
          onBlur={(e) => conv && patchConv({ title: e.target.value })} />
        {workspace && (
          <button className="chip hover:text-fg" onClick={() => go({ page: "projects", id: workspace.id })} title={workspace.path}>
            <Folder size={12} /> {workspace.name}
          </button>
        )}
        {conv && (
          <button className="btn-ghost" title="Xóa cuộc trò chuyện" onClick={async () => {
            if (!confirm("Xóa cuộc trò chuyện này?")) return;
            await api.del(`/api/desktop/conversations/${conv.id}`);
            shared.reloadConversations();
            go({ page: "chat" });
          }}><Trash2 size={15} /></button>
        )}
      </div>

      {/* messages */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-4 py-6">
          {messages.length === 0 && <Empty mode={mode} preset={p} />}
          {messages.map((m) => m.role === "user" ? (
            <div key={m.id} className="mb-5 flex flex-col items-end">
              {m.blocks.filter((b) => b.type === "text").map((b, i) => (
                <div key={i} className="max-w-[85%] whitespace-pre-wrap rounded-2xl bg-sunk px-4 py-2.5">{b.text}</div>
              ))}
              <Attachments blocks={m.blocks} />
            </div>
          ) : (
            <div key={m.id} className="mb-6">
              <div className="mb-1 flex items-center gap-2 text-[11px] text-muted">
                <Bot size={13} /> {m.preset}{m.model && m.model !== m.preset ? ` · ${m.model}` : ""}
                {m.usage?.tokens_out ? ` · ${m.usage.tokens_out} tokens` : ""}
                {m.usage?.seconds ? ` · ${m.usage.seconds}s` : ""}
                {m.status === "canceled" && " · đã hủy"}
                {m.status === "interrupted" && " · bị gián đoạn (khởi động lại)"}
              </div>
              <Blocks blocks={m.blocks} live={m.status === "streaming"} />
              {m.status === "streaming" && (
                <div className="mt-1 flex items-center gap-2 text-[12px] text-muted">
                  <Loader2 size={13} className="animate-spin" /> {m.live_status || (m.blocks.length ? "Đang trả lời..." : "Đang chờ model...")}
                </div>
              )}
              {m.job_id && m.status !== "streaming" && m.blocks.some((b) => b.type === "tool" && b.checkpoint) && (
                <button className="btn-ghost mt-1 text-xs" onClick={async () => {
                  if (!confirm("Khôi phục mọi file mà lượt này đã sửa về trạng thái trước đó?")) return;
                  const r = await api.post<{ restored: string[] }>(`/api/desktop/jobs/${m.job_id}/revert`);
                  alert(`Đã khôi phục: ${r.restored.join(", ") || "(không có)"}`);
                }}>↶ Hoàn tác thay đổi file của lượt này</button>
              )}
            </div>
          ))}
          <div ref={bottom} />
        </div>
      </div>

      {/* composer */}
      <div className="shrink-0 px-4 pb-4">
        <div className="mx-auto max-w-3xl">
          {err && <div className="mb-2 flex items-start gap-2 rounded-md border border-bad/40 bg-bad/5 p-2 text-[13px] text-bad">
            <span className="flex-1">{err}</span><button onClick={() => setErr(null)}><X size={14} /></button></div>}
          <div className="card p-2 shadow-sm">
            {atts.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {atts.map((a) => (
                  <span key={a.id} className="chip bg-sunk">{a.name}
                    <button onClick={() => setAtts(atts.filter((x) => x.id !== a.id))}><X size={11} /></button></span>
                ))}
              </div>
            )}
            {isMedia && showParams && p && (
              <div className="mb-2 rounded-md bg-sunk p-2"><ParamsForm preset={p} value={params} onChange={setParams} compact /></div>
            )}
            <textarea className="max-h-60 min-h-[56px] w-full resize-none bg-transparent px-2 py-1 outline-none"
              placeholder={isMedia ? "Mô tả ảnh / video / cảnh 3D cần tạo..." : mode === "agent"
                ? "Giao việc cho agent trong Project (đọc code, sửa, chạy test)..." : "Nhắn cho SHAMS... (Enter để gửi, Shift+Enter xuống dòng)"}
              value={text} onChange={(e) => setText(e.target.value)}
              onPaste={(e) => {
                if (e.clipboardData.files.length) {
                  e.preventDefault();
                  addFiles(e.clipboardData.files);
                }
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }} />
            {p?.content_policy?.adult && (
              <label className="mb-1 flex items-start gap-2 px-2 text-[12px] text-warn">
                <input type="checkbox" checked={confirmAdult} onChange={(e) => setConfirmAdult(e.target.checked)} className="mt-0.5" />
                Tôi đủ 18 tuổi; nội dung chỉ gồm người trưởng thành hư cấu, có đồng thuận, không phải người thật.
              </label>
            )}
            <div className="flex flex-wrap items-center gap-1.5">
              <input ref={fileInput} type="file" multiple hidden onChange={(e) => e.target.files && addFiles(e.target.files)} />
              <button className="btn-ghost" title="Đính kèm file / ảnh" onClick={() => fileInput.current?.click()}>
                {uploading ? <Loader2 size={16} className="animate-spin" /> : <Paperclip size={16} />}</button>
              <PresetPicker presets={shared.presets} value={preset} onChange={(v) => {
                setPreset(v);
                setParams({});
                if (conv) patchConv({ preset: v });
              }} />
              {!isMedia && (
                <div className="flex overflow-hidden rounded-md border border-line text-xs">
                  {(["chat", "agent"] as const).map((m) => (
                    <button key={m} className={`px-2 py-1 ${mode === m ? "bg-accent text-white" : "hover:bg-sunk"}`}
                      onClick={() => {
                        setMode(m);
                        if (conv) patchConv({ mode: m });
                      }}>{m === "chat" ? "Chat" : "Agent"}</button>
                  ))}
                </div>
              )}
              {mode === "agent" && !isMedia && (
                <>
                  <select className="input w-auto max-w-[180px] py-1 text-xs" value={ws} onChange={(e) => {
                    setWs(e.target.value);
                    if (conv) patchConv({ workspace_id: e.target.value || null });
                  }}>
                    <option value="">— Project —</option>
                    {shared.workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                  <select className="input w-auto py-1 text-xs" value={perm} title="Quyền của agent" onChange={(e) => {
                    setPerm(e.target.value);
                    if (conv) patchConv({ permission_mode: e.target.value as Conversation["permission_mode"] });
                  }}>
                    {PERMS.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
                  </select>
                  <ShieldCheck size={14} className="text-muted" />
                </>
              )}
              {isMedia && (
                <button className={`btn-ghost text-xs ${showParams ? "text-fg" : ""}`} onClick={() => setShowParams(!showParams)}>
                  <Settings2 size={14} /> Tham số</button>
              )}
              <div className="ml-auto">
                {busy ? (
                  <button className="btn" onClick={stop} title="Dừng"><Square size={14} /> Dừng</button>
                ) : (
                  <button className="btn-primary" disabled={!preset || (!text.trim() && !atts.length)} onClick={send}>
                    <ArrowUp size={16} /></button>
                )}
              </div>
            </div>
          </div>
          {!preset && <div className="mt-1.5 text-center text-[12px] text-muted">Chọn một model hoặc preset (Model Manager để thêm).</div>}
        </div>
      </div>
    </div>
  );
}

function Empty({ mode, preset }: { mode: string; preset?: Preset }) {
  return (
    <div className="mt-16 text-center">
      <img src="/logo.png" className="mx-auto mb-3 h-12 w-12 opacity-90" alt="" />
      <div className="text-xl font-semibold">{mode === "agent" ? "Agent coding trong Project" : "Bạn cần gì hôm nay?"}</div>
      <div className="mt-1 text-sm text-muted">
        {preset ? `${preset.label}${preset.model ? " · " + preset.model : ""}` : "Chọn model ở thanh dưới để bắt đầu"}
      </div>
      {mode === "agent" && (
        <div className="mx-auto mt-4 max-w-md text-left text-[13px] text-muted">
          Agent đọc và sửa file trong thư mục Project, chạy lệnh/test, và hỏi bạn trước các thao tác nhạy cảm
          (xóa, git push, cài đặt, mạng). Mọi thay đổi đều có diff và có thể hoàn tác.
        </div>
      )}
    </div>
  );
}
