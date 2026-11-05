export type Block = {
  type: "text" | "thinking" | "tool" | "image" | "video" | "model3d" | "audio" | "file" | "attachment" | "links"
    | "error" | "notice" | "task";
  text?: string;
  [k: string]: any;
};

export type Message = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system";
  blocks: Block[];
  status: "done" | "streaming" | "error" | "canceled" | "interrupted";
  model?: string | null;
  preset?: string | null;
  job_id?: string | null;
  usage?: Record<string, number>;
  created: number;
  live_status?: string;
};

export type Conversation = {
  id: string;
  title: string;
  mode: "chat" | "agent";
  preset: string | null;
  workspace_id: string | null;
  permission_mode: "ask" | "safe" | "auto";
  pinned: number;
  archived: number;
  created: number;
  updated: number;
  message_count?: number;
};

export type Preset = {
  id: string;
  label: string;
  description: string;
  kind: "chat" | "image" | "video" | "scene3d" | "image_to_3d" | string;
  provider: string | null;
  model: string | null;
  wanted: string | null;
  status: string;
  how: string;
  capabilities: string[];
  execution_modes: string[];
  autonomous_default: boolean;
  source: "role" | "preset";
  input_schema: { properties?: Record<string, any>; required?: string[] };
  params: Record<string, any>;
  content_policy: { adult?: boolean };
  resources: { lane: string; vram_mb: number | null };
};

export type WorkspaceRow = {
  id: string;
  name: string;
  path: string;
  env: Record<string, string>;
  shell: string | null;
  python: string | null;
  notes: string;
  exists?: boolean;
  git?: boolean;
};

export type Route =
  | { page: "chat"; id?: string }
  | { page: "history" }
  | { page: "direct" }
  | { page: "autonomous" }
  | { page: "projects"; id?: string }
  | { page: "media" }
  | { page: "models" }
  | { page: "settings" };
