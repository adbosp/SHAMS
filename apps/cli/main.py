"""`shams` CLI (ADR-112): human review and operation before the desktop UI exists.

  shams init <dir> --project-id my_app --goal "..." [--domain software|game|...] [--opt key=value]...
             [--engine unity|unreal|godot|web] [--mode guided] [--mock]
  shams plan <dir> [--template <name>] [--opt key=value]... --approve <name>
  shams packs [--dir <dir>]               # Domain Packs and their options (stack, engine, ...)
  shams engines [--dir <dir>]             # Game Pack engines (built-in + engines/*.toml)
  shams providers                         # model providers you can use in nodes.toml
  shams nodes <dir> [--check]             # nodes, lanes, workers from nodes.toml; --check pings providers
  shams run <dir> [--defect task_id:criterion_id[:fail_attempts]]...   # exit 0 done, 1 waiting/blocked, 2 error
  shams status <dir> [--json]
  shams review <dir>                      # list tasks waiting for a human
  shams decide <dir> <task_id> accept|retry|fail|cancel|unblock [--note ...]
  shams events <dir> [--task <id>] [--since N]
  shams templates [--dir <dir>]
  shams ui [workspace|project] [--port 8765] [--no-browser]   # the app at http://127.0.0.1:8765
  shams hardware [--json]                 # real GPUs / VRAM / RAM / CPU
  shams settings [<dir>] [--json]         # merged settings (user + project + local), validation
  shams desktop-backend [<dir>] [--port N]   # local API for the desktop client (Tauri)
  shams mcp [<dir>] [--name <server>]     # connect to MCP servers, list tools and permissions
  shams mcp --presets | --add <preset> [--as <name>]   # ready-made servers: searxng, browser, github, blender, unity
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shams.mock.worker import MockDefect, MockWorker
from shams.orchestration.project import MODES, Shams as Studio
from shams.planning.validator import list_templates
from shams.statemachine import S


def _open(d: str, defects: list[str] | None = None) -> Studio:
    path = Path(d)
    parsed = {}
    for spec in defects or []:
        parts = spec.split(":")
        parsed[parts[0]] = MockDefect(parts[1], int(parts[2]) if len(parts) > 2 else 1)
    return Studio(path, mock=MockWorker(path / "attempts", defects=parsed))


def _opts(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--opt expects KEY=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _print_status(s: dict) -> None:
    print(f"Project {s['project_id']}  [{s['setup']}]  mode={s['mode']}  mock={s['mock_mode']}  done={s['done']}")
    print("States: " + ", ".join(f"{k}={v}" for k, v in sorted(s["counts"].items())))
    print(f"Model loads={s['model_loads']}  unloads={s['model_unloads']}  repairs={s['repairs']}  events={s['events']}")
    for t in s["tasks"]:
        print(f"  {t['state']:<16} a{t['attempt']}  {t['task_id']:<32} {t['capability']}")


def main(argv: list[str] | None = None) -> int:
    from shams import __version__
    p = argparse.ArgumentParser(prog="shams", description="SHAMS controller CLI")
    p.add_argument("--version", action="version", version=f"shams {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init"); a.add_argument("dir"); a.add_argument("--project-id", required=True)
    a.add_argument("--goal", required=True); a.add_argument("--mode", choices=MODES, default="guided")
    a.add_argument("--mock", action="store_true", help="run without any AI model (Mock Worker)")
    a.add_argument("--domain", help="Domain Pack (see `shams packs`); default: detected from the goal")
    a.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE", help="pack option, e.g. stack=typescript")
    a.add_argument("--engine", help="Game Pack shortcut: unity | unreal (ue5) | godot | web (three.js/WebGPU)")
    a = sub.add_parser("plan"); a.add_argument("dir"); a.add_argument("--template", help="default: the pack's template")
    a.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE", help="change a pack option before the first plan")
    a.add_argument("--engine", help="Game Pack shortcut for --opt engine=...")
    a.add_argument("--approve", help="name of the person approving the plan")
    a.add_argument("--llm", action="store_true", help="let the planner model write the DAG (template as fallback)")
    a = sub.add_parser("run"); a.add_argument("dir"); a.add_argument("--defect", action="append")
    a.add_argument("--max-steps", type=int, default=500)
    a = sub.add_parser("status"); a.add_argument("dir"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("review"); a.add_argument("dir")
    a = sub.add_parser("decide"); a.add_argument("dir"); a.add_argument("task_id")
    a.add_argument("decision", choices=["accept", "retry", "fail", "cancel", "unblock"]); a.add_argument("--note", default="")
    a = sub.add_parser("events"); a.add_argument("dir"); a.add_argument("--task"); a.add_argument("--since", type=int, default=0)
    a = sub.add_parser("templates"); a.add_argument("--dir")
    a = sub.add_parser("engines"); a.add_argument("--dir")
    a = sub.add_parser("packs"); a.add_argument("--dir")
    sub.add_parser("providers")
    a = sub.add_parser("nodes"); a.add_argument("dir"); a.add_argument("--check", action="store_true")
    a = sub.add_parser("ui", help="open the SHAMS app (local web server)")
    a.add_argument("dir", nargs="?", help="workspace folder or a project folder (default: $SHAMS_WORKSPACE or ~/SHAMS)")
    a.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a.add_argument("--host", default="127.0.0.1"); a.add_argument("--port", type=int, default=8765)
    a.add_argument("--mock-delay", type=float, default=0.8, help="seconds of simulated work per mock task")
    a = sub.add_parser("desktop-backend", help="run the local API for the SHAMS desktop client (no browser)")
    a.add_argument("dir", nargs="?", help="workspace folder (default: $SHAMS_WORKSPACE or ~/SHAMS)")
    a.add_argument("--host", default="127.0.0.1"); a.add_argument("--port", type=int, default=8765)
    a.add_argument("--mock-delay", type=float, default=0.8)
    a = sub.add_parser("hardware", help="GPUs / VRAM / RAM / CPU of this PC"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("settings", help="show merged settings (user + project) and check them")
    a.add_argument("dir", nargs="?"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("mcp", help="connect to the configured MCP servers and list their tools")
    a.add_argument("dir", nargs="?"); a.add_argument("--name")
    a.add_argument("--presets", action="store_true", help="list ready-made servers (ADR-123)")
    a.add_argument("--add", metavar="PRESET", help="add a preset to user settings (or project with dir)")
    a.add_argument("--as", dest="as_name", metavar="NAME", help="server name for --add (default: preset id)")
    a = sub.add_parser("models", help="discover installed models/tools and show which model serves each role")
    a.add_argument("dir", nargs="?"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("selftest", help="run the integration tests on this machine (real models and tools)")
    a.add_argument("--only", help="comma-separated test ids, e.g. 1,3,4b")
    a.add_argument("--out", help="report folder (default: <workspace>/_selftest/<time>)")
    a.add_argument("--workspace", help="default: $SHAMS_WORKSPACE or ~/SHAMS")
    a = sub.add_parser("presets", help="models / presets you can run directly (Direct Mode)")
    a.add_argument("dir", nargs="?"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("direct", help="run one prompt on one chosen preset, without planner or DAG")
    a.add_argument("prompt"); a.add_argument("--preset", required=True)
    a.add_argument("--param", action="append", default=[], help="k=v (JSON values allowed), repeatable")
    a.add_argument("--dir", help="project folder (default: <workspace>/direct, created if missing)")
    a.add_argument("--confirm-adult", action="store_true",
                   help="adult-only presets: I am 18+ and the content involves only fictional consenting adults")
    a.add_argument("--json", action="store_true")
    a = sub.add_parser("toolchain", help="test toolchains SHAMS installs in its own folder (e.g. web: Vitest + Three.js)")
    a.add_argument("action", choices=["status", "install"]); a.add_argument("name", nargs="?", default="web", choices=["web"])
    args = p.parse_args(argv)

    if args.cmd == "toolchain":
        from shams_packs.game import toolchain
        if args.action == "install":
            r = toolchain.install_web()
            print(r["log"][-1500:])
            print(("installed in " if r["ok"] else "install FAILED in ") + r["dir"])
            return 0 if r["ok"] else 1
        st = toolchain.status()
        print(f"web: {'installed' if st['installed'] else 'not installed'} ({st['dir']})")
        for k, v in st["versions"].items():
            print(f"  {k} {v or '-'}")
        return 0

    if args.cmd == "presets":
        from shams import settings as settings_mod
        from shams.models import ModelRegistry
        ps = ModelRegistry(settings_mod.load(args.dir)).presets()
        if args.json:
            print(json.dumps(ps, ensure_ascii=False, indent=2))
            return 0
        for x in ps:
            flags = ",".join(x["execution_modes"]) + (" 18+" if x["content_policy"].get("adult") else "")
            print(f"  {x['id']:<24} {x['kind']:<8} {str(x['provider']):<14} {str(x['model'] or x['wanted'])[:44]:<44} "
                  f"{x['status']:<14} [{flags}]")
        return 0
    if args.cmd == "direct":
        import os
        from shams import policy
        ws = Path(os.environ.get("SHAMS_WORKSPACE") or Path.home() / "SHAMS")
        d = Path(args.dir) if args.dir else ws / "direct"
        if not (d / "project.json").exists():
            Studio.init_project(d, "direct", "Direct Mode tasks", mode="guided", domain="software",
                                domain_config={"stack": "python"})
        params = {"prompt": args.prompt}
        for k, v in _opts(args.param).items():
            try:
                params[k] = json.loads(v)
            except json.JSONDecodeError:
                params[k] = v
        st = Studio(d)
        try:
            if args.confirm_adult:
                print("Confirmed: " + policy.CONFIRM_TEXT, file=sys.stderr)
            tid = st.direct(args.preset, params, by="cli", confirm_adult=args.confirm_adult)
            print(f"{tid} queued on {args.preset} ...", file=sys.stderr, flush=True)
            res = st.run_task(tid)
            files = [{"logical_id": a["logical_id"], "media_type": a["media_type"],
                      "file": str(st.artifacts.blob_path(a["sha256"]))} for a in res["outputs"]]
        except (KeyError, ValueError, PermissionError, RuntimeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        finally:
            st.close()
        if args.json:
            print(json.dumps({**res, "files": files}, ensure_ascii=False, indent=2, default=str))
        else:
            h = res.get("handoff") or {}
            print(f"{tid}: {res['state']}  {h.get('blocked_reason') or ''}")
            for f in files:
                if f["media_type"].startswith("text/"):
                    print(Path(f["file"]).read_text(encoding="utf-8"))
                print(f"  {f['logical_id']}  {f['media_type']}  {f['file']}")
        return 0 if res["state"] == "ACCEPTED" else 1

    if args.cmd == "models":
        from shams import settings as settings_mod
        from shams.models import ModelRegistry
        rep = ModelRegistry(settings_mod.load(args.dir)).report()
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0
        o = rep["ollama"]
        print(f"Ollama {o.get('version', '')} at {o.get('base_url', '?')}: {o.get('status')}"
              + (f"  ({o['error']})" if o.get("error") else ""))
        for m in o.get("models", []):
            loaded = any(x["name"] == m["name"] for x in o.get("loaded", []))
            print(f"  {m['name']:<44} {m['size_mb'] / 1024:5.1f} GB  {m.get('parameters') or '':>7} "
                  f"{m.get('quantization') or '':<7} {','.join(m.get('capabilities') or [])}{'  [loaded]' if loaded else ''}")
        print("Roles:")
        if not rep["roles"]:
            print("  none configured (settings: models.roles; see shams/examples/local_models.settings.json)")
        for r in rep["roles"]:
            print(f"  {r['role']:<12} {str(r['wanted']):<26} -> {str(r['model_id']):<44} {r['status']} {r['how']}")
        print("Tools:")
        for name, t in rep["tools"].items():
            print(f"  {name:<15} {t.get('status')}  " + json.dumps({k: v for k, v in t.items() if k != 'status'},
                                                                   ensure_ascii=False)[:160])
        return 0 if all(r["status"] in ("ok", "disabled") for r in rep["roles"]) else 1
    if args.cmd == "selftest":
        import os
        from shams import settings as settings_mod
        from shams_tools import selftest
        ws = args.workspace or os.environ.get("SHAMS_WORKSPACE") or str(Path.home() / "SHAMS")
        out = Path(args.out) if args.out else selftest.default_out(ws)
        only = {x.strip() for x in args.only.split(",")} if args.only else None
        rep = selftest.run(settings_mod.load(), out, only, log=lambda m: print(m, flush=True))
        print(f"\n{rep['counts']}  report: {out / 'report.md'}")
        return 1 if rep["counts"]["FAIL"] else 0

    if args.cmd == "ui":
        try:
            from apps.api.server import serve
        except ImportError:
            print('error: install the UI extra first: pip install -e ".[ui]"', file=sys.stderr)
            return 2
        import os
        target = args.dir or os.environ.get("SHAMS_WORKSPACE") or str(Path.home() / "SHAMS")
        print("Ctrl+C to stop")
        serve(target, args.host, args.port, args.mock_delay, open_browser=False if args.no_browser else None)
        return 0
    if args.cmd == "desktop-backend":
        import os
        from apps.api.server import serve
        target = args.dir or os.environ.get("SHAMS_WORKSPACE") or str(Path.home() / "SHAMS")
        # the desktop shell passes a random token; the API then refuses requests without it (ADR-122)
        serve(target, args.host, args.port, args.mock_delay, open_browser=False,
              token=os.environ.get("SHAMS_DESKTOP_TOKEN") or None, ready_line=True)
        return 0
    if args.cmd == "hardware":
        from shams import hardware
        hw = hardware.detect()
        if args.json:
            print(json.dumps(hw, ensure_ascii=False, indent=2))
            return 0
        print(f"{hw['hostname']}  {hw['os']}  CPU: {hw['cpu']['name']} ({hw['cpu']['cores']} threads)")
        ram = hw["ram"]
        if ram["total_mb"]:
            print(f"RAM: {ram['total_mb'] / 1024:.1f} GB" + (f" ({ram['available_mb'] / 1024:.1f} GB free)"
                                                          if ram["available_mb"] else ""))
        for g in hw["gpus"]:
            used = f"{g['vram_used_mb'] / 1024:.1f} / " if g["vram_used_mb"] is not None else ""
            print(f"GPU {g['index']}: {g['name']}  VRAM {used}{g['vram_total_mb'] / 1024:.1f} GB  [{g['source']}]")
        if not hw["gpus"]:
            print("GPU: none detected (SHAMS still runs; attach models on another node or use APIs)")
        return 0
    if args.cmd == "settings":
        from shams import settings as settings_mod
        s = settings_mod.load(args.dir)
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            for k, v in settings_mod.sources(args.dir).items():
                print(f"{k:<8} {v}{'' if Path(v).exists() else '  (not created)'}")
            print(f"permissions: {s['permissions']['mode']}  allow={s['permissions']['allow']}  deny={s['permissions']['deny']}")
            print(f"connections: {', '.join(s['connections']) or '-'}")
            print(f"workers:     {', '.join(w.get('id', '?') for w in s['workers']) or '-'}")
            print(f"mcp servers: {', '.join(s['mcp_servers']) or '-'}")
            print(f"hooks:       {', '.join(s['hooks']) or '-'}")
            for name, ok in settings_mod.env_status(s).items():
                print(f"env {name}: {'set' if ok else 'NOT SET'}")
        errs = settings_mod.validate(s)
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 2 if errs else 0
    if args.cmd == "mcp":
        from shams import settings as settings_mod
        from shams.mcp import MCPManager
        from shams_tools import mcp_catalog
        if args.presets:
            for p in mcp_catalog.list_presets():
                print(f"{p['id']:<12} {p['type']:<6} {p['title']}{'  [GPU]' if p['gpu'] else ''}")
                print(f"{'':<19} needs: {p['requires']}")
            return 0
        if args.add:
            scope = "project" if args.dir else "user"
            try:
                patch = mcp_catalog.settings_patch(args.add, args.as_name, current=settings_mod.read_scope(scope, args.dir))
            except KeyError as e:
                print(f"error: {e.args[0]}", file=sys.stderr)
                return 2
            path = settings_mod.update(scope, patch, args.dir)
            print(f"added MCP server {args.as_name or args.add!r} to {path}")
            print("check it with: shams mcp" + (f" {args.dir}" if args.dir else "") + f" --name {args.as_name or args.add}")
            return 0
        mgr = MCPManager.from_settings(settings_mod.load(args.dir))
        if args.name:
            mgr.servers = {k: v for k, v in mgr.servers.items() if k == args.name}
        if not mgr.servers:
            print("No MCP servers. Add them in Settings → MCP, or a .mcp.json file (mcpServers).")
            return 0
        bad = 0
        for row in mgr.status():
            if row["ok"]:
                print(f"{row['name']:<16} ok   {len(row['tools'])} tools")
                for t in row["tools"]:
                    flag = "allowed" if t["allowed"] else "needs permission"
                    print(f"    {t['qualified']:<48} {flag}{' (read-only)' if t['read_only'] else ''}")
            else:
                bad += 1
                print(f"{row['name']:<16} DOWN {row['error']}")
        mgr.close()
        return 1 if bad else 0

    if args.cmd == "templates":
        print("\n".join(list_templates(args.dir)))
        return 0
    if args.cmd == "providers":
        from shams import providers
        for name in providers.available():
            cls, cfg = providers.PRESETS.get(name, (None, {}))
            cfg = {"base_url": getattr(cls, "default_base_url", "") or None,
                   "api_key_env": getattr(cls, "default_key_env", None), **cfg}
            extra = " ".join(f"{k}={v}" for k, v in cfg.items() if v)
            print(f"{name:<18} {extra}")
        return 0
    if args.cmd == "packs":
        from shams import packs
        for p in packs.all_packs():
            print(f"{p.name:<10} {p.display:<10} {p.description}")
            for opt, choices in p.options(args.dir).items():
                print(f"    {opt}: " + ", ".join(c["id"] for c in choices))
        return 0
    if args.cmd == "engines":
        from shams import packs
        try:
            game = packs.get("game")
            game.load_plugins(args.dir)
        except (ValueError, KeyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        from shams_packs.game import engines
        for e in engines.ENGINES.values():
            print(f"{e.name:<7} {e.display:<20} {e.language:<11} {e.code_capability:<22} {e.build_target}")
        return 0
    if args.cmd == "init":
        try:
            opts = _opts(args.opt)
            Studio.init_project(args.dir, args.project_id, args.goal, mode=args.mode, mock_mode=args.mock,
                                domain=args.domain, domain_config=opts, engine=args.engine)
        except (KeyError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        import shams.examples
        example = Path(shams.examples.__file__).parent / "nodes.example.toml"
        if example.exists() and not (Path(args.dir) / "nodes.toml").exists():
            (Path(args.dir) / "nodes.example.toml").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        st = Studio(args.dir)
        print(f"Initialised {args.project_id} in {args.dir} ({st.describe()}, mode={args.mode}, mock={args.mock})")
        st.close()
        if not args.mock:
            print("Next: copy nodes.example.toml to nodes.toml and declare your models/APIs (see `shams providers`).")
        return 0
    st = _open(args.dir, getattr(args, "defect", None))
    try:
        if args.cmd == "plan":
            opts = _opts(args.opt)
            if args.engine:
                opts["engine"] = args.engine
            if opts:
                st.configure(**opts)
            dag = (st.plan_from_llm(approved_by=args.approve) if args.llm
                   else st.plan_from_template(args.template, approved_by=args.approve))
            from shams.planning.validator import validate_dag
            for i in validate_dag(dag, st.nodes if not st.mock_mode else None):
                print(i)
            print(f"Loaded {len(st.db.tasks())} tasks from template {dag['template']} ({st.describe()})")
        elif args.cmd == "nodes":
            for n in st.nodes.nodes.values():
                gpus = ", ".join(f"gpu{g['index']}={g['vram_mb']}MB" for g in n.get("gpus", [])) or "no GPU"
                print(f"node   {n['node_id']:<22} {n['kind']:<9} {gpus}")
            for ln in st.nodes.lanes():
                print(f"lane   {ln.lane_id:<22} {ln.kind:<9} capacity={ln.capacity}")
            for w in st.nodes.workers.values():
                ref = w.ref
                line = (f"worker {w.worker_id:<22} {ref.get('provider', ref['kind']):<12} {w.model or '-':<24} "
                        f"{w.lane_kind}@{w.node_id}  {', '.join(w.capabilities)}")
                if args.check and ref["kind"] != "mock":
                    try:
                        prov = getattr(st.worker_impl(w), "provider", None)
                        line += "  [" + (prov.health() if hasattr(prov, "health") else "no health check") + "]"
                    except Exception as e:  # report, keep listing
                        line += f"  [down: {e}]"
                print(line)
            if not st.nodes.workers:
                print("No workers. Create nodes.toml from nodes.example.toml (or run with --mock).")
            missing = sorted({t["capability"] for t in st.db.tasks()} - {c for w in st.nodes.workers.values()
                                                                          for c in w.capabilities})
            if missing:
                print("Capabilities with no worker (tasks will be BLOCKED): " + ", ".join(missing))
        elif args.cmd == "run":
            if not st.db.tasks():
                print("error: no plan loaded; run `shams plan <dir> --approve <name>` first", file=sys.stderr)
                return 2
            s = st.run(args.max_steps)
            _print_status(s)
            if not s["done"]:
                print("Not finished: see `shams review` for tasks waiting on a human.", file=sys.stderr)
                return 1
        elif args.cmd == "status":
            s = st.status()
            print(json.dumps(s, ensure_ascii=False, indent=2)) if args.json else _print_status(s)
        elif args.cmd == "review":
            waiting = st.db.tasks({S.HUMAN_REVIEW, S.BLOCKED})
            if not waiting:
                print("Nothing waiting for review.")
            for t in waiting:
                fs = st.db.failures(t["task_id"])
                last = fs[-1] if fs else None
                print(f"{t['state']:<13} {t['task_id']} (attempt {t['attempt']})")
                if last:
                    print(f"    last failure: {last['failure_class']} — {last['summary'][:120]}")
                    print(f"    plan: {last.get('repair_plan', {}).get('notes', '')}")
        elif args.cmd == "decide":
            st.human_decide(args.task_id, args.decision, args.note)
            print(f"{args.task_id}: {args.decision} -> {st.db.state(args.task_id)}")
        elif args.cmd == "events":
            for e in st.db.events(args.since, args.task):
                arrow = f" {e['from_state'] or ''}->{e['to_state']}" if e["to_state"] else ""
                print(f"{e['seq']:>5} {e['ts']} {e['type']:<22} {e['subject_id']}{arrow}")
    except (ValueError, PermissionError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
