// Connection to the SHAMS Core Service (local FastAPI). In the desktop shell the Rust side starts the
// Python backend and tells us its URL + token; in a plain browser (dev) VITE_SHAMS_API is used.
import type { Block, Conversation, Message } from "./types";

export type BackendInfo = { url: string; token: string | null; managed: boolean; log?: string | null };

let info: BackendInfo | null = null;

export const inTauri = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export async function backendInfo(force = false): Promise<BackendInfo> {
  if (info && !force) return info;
  if (inTauri()) {
    const { invoke } = await import("@tauri-apps/api/core");
    info = await invoke<BackendInfo>("backend_info");
  } else {
    const url = (import.meta.env.VITE_SHAMS_API as string | undefined) || "http://127.0.0.1:8765";
    info = { url, token: (import.meta.env.VITE_SHAMS_TOKEN as string | undefined) || null, managed: false };
  }
  return info;
}

export function setBackend(b: BackendInfo) {
  info = b;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(method: string, path: string, body?: unknown, raw?: BodyInit, ctype?: string): Promise<T> {
  const b = await backendInfo();
  const headers: Record<string, string> = {};
  if (b.token) headers["X-Shams-Token"] = b.token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (ctype) headers["Content-Type"] = ctype;
  const r = await fetch(b.url + path, { method, headers, body: raw ?? (body !== undefined ? JSON.stringify(body) : undefined) });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg);
  }
  return (await r.json()) as T;
}

export const api = {
  get: <T,>(p: string) => req<T>("GET", p),
  post: <T,>(p: string, body: unknown = {}) => req<T>("POST", p, body),
  patch: <T,>(p: string, body: unknown) => req<T>("PATCH", p, body),
  put: <T,>(p: string, body: unknown) => req<T>("PUT", p, body),
  del: <T,>(p: string) => req<T>("DELETE", p),
  upload: (file: File) =>
    req<{ id: string; name: string; mime: string; size: number; url: string }>(
      "POST", `/api/desktop/uploads?name=${encodeURIComponent(file.name)}`, undefined, file, file.type || "application/octet-stream"),
};

/** Absolute URL for a backend path (images, downloads); adds the token for <img>/<video> tags. */
export function fileUrl(path: string): string {
  if (!info || /^https?:\/\//.test(path) || path.startsWith("data:")) return path;
  const sep = path.includes("?") ? "&" : "?";
  return info.url + path + (info.token ? `${sep}token=${encodeURIComponent(info.token)}` : "");
}

export type StreamEvent = { type: string; seq: number; [k: string]: any };

/** Server-Sent Events for one job; reconnects after a drop and resumes after the last event seen. */
export function streamJob(jobId: string, on: (ev: StreamEvent) => void, onEnd: () => void): () => void {
  let closed = false;
  let es: EventSource | null = null;
  let last = 0;
  const open = () => {
    if (closed || !info) return;
    const tok = info.token ? `&token=${encodeURIComponent(info.token)}` : "";
    es = new EventSource(`${info.url}/api/desktop/jobs/${jobId}/stream?after=${last}${tok}`);
    const handler = (e: MessageEvent) => {
      const data = JSON.parse(e.data || "{}");
      if (typeof data.seq === "number") last = data.seq;
      on({ ...data, type: e.type });
    };
    for (const t of ["message", "delta", "thinking", "tool", "approval", "media", "status", "task", "usage", "error", "done"])
      es.addEventListener(t, handler as EventListener);
    es.addEventListener("end", () => {
      closed = true;
      es?.close();
      onEnd();
    });
    es.addEventListener("gone", () => {
      closed = true;
      es?.close();
      onEnd();
    });
    es.onerror = () => {
      es?.close();
      if (!closed) setTimeout(open, 1000);
    };
  };
  open();
  return () => {
    closed = true;
    es?.close();
  };
}

/** Generic SSE subscription (autonomous state). */
export function subscribe(path: string, event: string, on: (data: any) => void): () => void {
  let closed = false;
  let es: EventSource | null = null;
  const open = () => {
    if (closed || !info) return;
    const sep = path.includes("?") ? "&" : "?";
    es = new EventSource(info.url + path + (info.token ? `${sep}token=${encodeURIComponent(info.token)}` : ""));
    es.addEventListener(event, (e) => on(JSON.parse((e as MessageEvent).data)));
    es.onerror = () => {
      es?.close();
      if (!closed) setTimeout(open, 2000);
    };
  };
  open();
  return () => {
    closed = true;
    es?.close();
  };
}

// --------------------------------------------------------------------------- live message assembly
/** Apply one stream event to an assistant message being built in the UI (same shape the server saves). */
export function applyEvent(msg: Message, ev: StreamEvent): Message {
  const blocks: Block[] = [...(msg.blocks || [])];
  const at = (i: number, fallback: Block) => {
    while (blocks.length <= i) blocks.push({ type: "text", text: "" });
    if (blocks[i].type !== fallback.type) blocks[i] = fallback;
    return blocks[i];
  };
  switch (ev.type) {
    case "delta": {
      const b = at(ev.index, { type: "text", text: "" });
      blocks[ev.index] = { ...b, text: (b.text || "") + ev.text };
      break;
    }
    case "thinking": {
      const b = at(ev.index, { type: "thinking", text: "" });
      blocks[ev.index] = { ...b, text: (b.text || "") + ev.text };
      break;
    }
    case "tool":
    case "approval":
      while (blocks.length <= ev.index) blocks.push({ type: "text", text: "" });
      blocks[ev.index] = ev.block;
      break;
    case "media":
      blocks.push(ev.block);
      break;
    case "task":
      blocks.push({ type: "task", task_id: ev.task_id, state: ev.state });
      break;
    case "status":
      return { ...msg, blocks, live_status: ev.text };
    case "usage":
      return { ...msg, blocks, usage: ev.usage };
    case "done":
      return { ...(ev.message as Message), live_status: undefined };
    case "error":
      return { ...msg, blocks: [...blocks, { type: "error", text: ev.text }] };
  }
  return { ...msg, blocks };
}

export type ConversationDetail = {
  conversation: Conversation;
  messages: Message[];
  active_jobs: string[];
  pending_approvals: any[];
};
