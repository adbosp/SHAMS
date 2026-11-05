import { stripTypeScriptTypes } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
let bad = 0;
for (const f of process.argv.slice(2)) {
  try {
    let src = readFileSync(f, "utf8");
    if (/\.tsx?$/.test(f)) src = stripTypeScriptTypes(src, { mode: "transform" });
    writeFileSync(f + ".check.mjs", src);
    execFileSync(process.execPath, ["--check", f + ".check.mjs"], { stdio: "pipe" });
  } catch (e) { bad++; console.log(`${f}: ${(e.stderr?.toString() || e.message).split("\n").slice(0, 6).join(" ")}`); }
}
process.exit(bad ? 1 : 0);
