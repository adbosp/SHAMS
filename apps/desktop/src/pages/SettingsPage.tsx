import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { api, backendInfo, inTauri, type BackendInfo } from "../lib/api";
import { useData, type Theme } from "../lib/store";

type SettingsView = { merged: any; scopes: Record<string, any>; errors: string[]; sources: Record<string, string> };

export default function SettingsPage({ theme, setTheme, health }: {
  theme: Theme; setTheme: (t: Theme) => void; health: { version: string; workspace: string; pid: number };
}) {
  const { data, reload } = useData<SettingsView>("/api/settings");
  const [b, setB] = useState<BackendInfo | null>(null);
  const [raw, setRaw] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [shellCfg, setShellCfg] = useState<any>(null);
  useEffect(() => { backendInfo().then(setB); }, []);
  useEffect(() => { if (data) setRaw(JSON.stringify(data.scopes.user || {}, null, 2)); }, [data]);
  useEffect(() => {
    if (!inTauri()) return;
    import("@tauri-apps/api/core").then(({ invoke }) => invoke("shell_config").then(setShellCfg).catch(() => undefined));
  }, []);

  const user = data?.scopes.user || {};
  const desktop = user.desktop || {};
  const resources = user.resources || {};
  const patch = async (path: string[], value: unknown) => {
    const next = JSON.parse(JSON.stringify(user));
    let o = next;
    for (const k of path.slice(0, -1)) o = o[k] ||= {};
    o[path[path.length - 1]] = value;
    await save(next);
  };
  const save = async (obj: any) => {
    try {
      await api.put("/api/settings", { scope: "user", data: obj });
      setMsg("Đã lưu");
      reload();
    } catch (e: any) {
      setMsg("Lỗi: " + e.message);
    }
    setTimeout(() => setMsg(null), 2500);
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-5">
        <h1 className="text-xl font-semibold">Settings</h1>
        {msg && <div className={`mt-2 text-sm ${msg.startsWith("Lỗi") ? "text-bad" : "text-ok"}`}>{msg}</div>}

        <Section title="Giao diện">
          <div className="flex gap-2">
            {(["system", "light", "dark"] as Theme[]).map((t) => (
              <button key={t} className={theme === t ? "btn-primary" : "btn"} onClick={() => setTheme(t)}>
                {t === "system" ? "Theo hệ thống" : t === "light" ? "Sáng" : "Tối"}</button>
            ))}
          </div>
        </Section>

        <Section title="Chung">
          <Toggle label="Tự mở game khi build xong" hint="Mở link chơi thử trong trình duyệt khi task build được nhận."
            on={user.general?.open_game !== false} set={(v) => patch(["general", "open_game"], v)} />
          <Toggle label="Cho phép preset 18+" hint="Chỉ dùng trong Direct AI, mỗi lần chạy vẫn phải xác nhận; không bao giờ chạy tự động. Nội dung về trẻ vị thành niên, không đồng thuận hoặc người thật luôn bị từ chối."
            on={!!user.general?.allow_adult_content} set={(v) => patch(["general", "allow_adult_content"], v)} />
          <label className="mt-2 flex items-center justify-between gap-3 text-sm">
            <span>Chế độ mặc định của dự án Autonomous</span>
            <select className="input w-auto" value={user.general?.default_mode || "autonomous"} onChange={(e) => patch(["general", "default_mode"], e.target.value)}>
              <option value="autonomous">autonomous (tự chạy đến cuối)</option>
              <option value="guided">guided</option>
              <option value="collaborative">collaborative</option>
            </select>
          </label>
        </Section>

        <Section title="Quyền của agent (mặc định cho cuộc trò chuyện mới)">
          <select className="input w-auto" value={desktop.permission_mode || "ask"} onChange={(e) => patch(["desktop", "permission_mode"], e.target.value)}>
            <option value="ask">Ask Before Action — hỏi trước mỗi thay đổi/lệnh</option>
            <option value="safe">Allow Safe Workspace Actions — tự sửa file trong Project, chạy lệnh an toàn</option>
            <option value="auto">Autonomous With Guardrails — tự làm, trừ thao tác phá hủy/nhạy cảm</option>
          </select>
          <p className="mt-1 text-xs text-muted">Ở mọi chế độ: xóa file, git push/reset, cài đặt, mạng, tắt tiến trình luôn cần bạn duyệt; đường dẫn ngoài Project bị từ chối.</p>
          <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
            <label>Số vòng tool tối đa
              <input className="input mt-1" type="number" defaultValue={desktop.agent_max_rounds ?? 30}
                onBlur={(e) => patch(["desktop", "agent_max_rounds"], parseInt(e.target.value, 10) || 30)} /></label>
            <label>Timeout lệnh (giây)
              <input className="input mt-1" type="number" defaultValue={desktop.command_timeout ?? 120}
                onBlur={(e) => patch(["desktop", "command_timeout"], parseInt(e.target.value, 10) || 120)} /></label>
            <label className="col-span-2">Thư mục tạo Project mới
              <input className="input mt-1 font-mono text-xs" defaultValue={desktop.projects_root ?? ""} placeholder="(mặc định: cạnh workspace, SHAMS_Projects)"
                onBlur={(e) => patch(["desktop", "projects_root"], e.target.value || null)} /></label>
          </div>
        </Section>

        <Section title="Tài nguyên GPU">
          <select className="input w-auto" value={String(resources.serialize_gpu ?? "auto")}
            onChange={(e) => patch(["resources", "serialize_gpu"], e.target.value === "auto" ? "auto" : e.target.value === "true")}>
            <option value="auto">Tự động (1 GPU: chạy tuần tự từng job nặng)</option>
            <option value="true">Luôn tuần tự</option>
            <option value="false">Không giới hạn (nhiều GPU)</option>
          </select>
          <p className="mt-1 text-xs text-muted">Có hiệu lực sau khi khởi động lại backend.</p>
        </Section>

        <Section title="Backend (SHAMS Core Service)">
          <div className="grid grid-cols-[140px_1fr] gap-1 text-sm">
            <span className="text-muted">Phiên bản</span><span>{health.version} (pid {health.pid})</span>
            <span className="text-muted">Địa chỉ</span><span className="font-mono text-xs">{b?.url}</span>
            <span className="text-muted">Quản lý bởi app</span><span>{b?.managed ? "có" : "không (đã chạy sẵn)"}</span>
            <span className="text-muted">Workspace</span><span className="font-mono text-xs">{health.workspace}</span>
            {shellCfg && <><span className="text-muted">Python</span><span className="font-mono text-xs">{shellCfg.python}</span>
              <span className="text-muted">Thư mục SHAMS</span><span className="font-mono text-xs">{shellCfg.shams_dir}</span>
              <span className="text-muted">File cấu hình</span><span className="font-mono text-xs">{shellCfg.config_path}</span></>}
          </div>
        </Section>

        <Section title="settings.json (người dùng)">
          {data?.errors?.length ? <div className="mb-2 text-xs text-bad">{data.errors.join("; ")}</div> : null}
          <textarea className="input h-80 font-mono text-xs" value={raw} onChange={(e) => setRaw(e.target.value)} spellCheck={false} />
          <button className="btn-primary mt-2" onClick={() => {
            try { save(JSON.parse(raw)); } catch (e: any) { setMsg("Lỗi: JSON không hợp lệ: " + e.message); }
          }}><Save size={14} /> Lưu settings</button>
          <p className="mt-1 text-xs text-muted">Không lưu API key ở đây; dùng api_key_env (tên biến môi trường).</p>
        </Section>
      </div>
    </div>
  );
}

function Toggle({ label, hint, on, set }: { label: string; hint: string; on: boolean; set: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between gap-4 py-1.5">
      <div><div className="text-sm">{label}</div><div className="text-xs text-muted">{hint}</div></div>
      <button role="switch" aria-checked={on} onClick={() => set(!on)}
        className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition ${on ? "bg-accent" : "bg-line"}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
      </button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="card mt-4 p-4">
      <h2 className="mb-2 font-medium">{title}</h2>
      {children}
    </section>
  );
}
