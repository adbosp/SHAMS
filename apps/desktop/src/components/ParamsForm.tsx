import type { Preset } from "../lib/types";

const LABELS: Record<string, string> = {
  negative: "Negative prompt", width: "Rộng", height: "Cao", steps: "Steps", cfg: "CFG", seed: "Seed",
  lora_strength: "LoRA", length: "Số frame", fps: "FPS", temperature: "Temperature", max_tokens: "Max tokens",
  num_ctx: "Context", builder: "Builder", system: "System prompt",
};

/** Inputs for a preset's input_schema (everything except the prompt, which the composer owns). */
export default function ParamsForm({ preset, value, onChange, compact }: {
  preset: Preset; value: Record<string, any>; onChange: (v: Record<string, any>) => void; compact?: boolean;
}) {
  const props = preset.input_schema?.properties || {};
  const keys = Object.keys(props).filter((k) => k !== "prompt" && !["research", "think"].includes(k));
  if (!keys.length) return null;
  return (
    <div className={`grid gap-2 ${compact ? "grid-cols-3 md:grid-cols-6" : "grid-cols-2 md:grid-cols-4"}`}>
      {keys.map((k) => {
        const s = props[k];
        const def = preset.params?.[k];
        const long = k === "negative" || k === "system";
        return (
          <label key={k} className={`text-xs ${long ? "col-span-full" : ""}`}>
            <span className="mb-0.5 block text-muted">{LABELS[k] || k}</span>
            {long ? (
              <textarea className="input h-14 resize-y" value={value[k] ?? ""} placeholder={def ? String(def) : ""}
                onChange={(e) => onChange({ ...value, [k]: e.target.value || undefined })} />
            ) : (
              <input className="input py-1" type={s.type === "string" ? "text" : "number"} value={value[k] ?? ""}
                placeholder={def != null ? String(def) : s.minimum != null ? `${s.minimum}–${s.maximum ?? ""}` : ""}
                step={s.type === "number" ? "any" : s.multipleOf || 1}
                onChange={(e) => {
                  const raw = e.target.value;
                  const v = raw === "" ? undefined : s.type === "integer" ? parseInt(raw, 10) : s.type === "number" ? parseFloat(raw) : raw;
                  onChange({ ...value, [k]: v });
                }} />
            )}
          </label>
        );
      })}
    </div>
  );
}
