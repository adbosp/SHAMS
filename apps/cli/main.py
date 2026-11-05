"""`shams` CLI (ADR-112): human review and operation before the desktop UI exists.

  shams init <dir> --project-id my_game --goal "..." [--engine unity|unreal|godot|web] [--mode guided] [--mock]
  shams plan <dir> [--template vertical_slice_3d] [--engine ...] --approve <name>
  shams engines [--dir <dir>]             # built-in + custom engines (engines/*.toml)
  shams providers                         # model providers you can use in nodes.toml
  shams nodes <dir> [--check]             # nodes, lanes, workers from nodes.toml; --check pings providers
  shams run <dir> [--defect task_id:criterion_id[:fail_attempts]]...
  shams status <dir> [--json]
  shams review <dir>                      # list tasks waiting for a human
  shams decide <dir> <task_id> accept|retry|fail|cancel|unblock [--note ...]
  shams events <dir> [--task <id>] [--since N]
  shams templates [--dir <dir>]
  shams ui <dir> [--port 8765] [--mock-delay 0.8]   # main UI at http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from studio.mock.worker import MockDefect, MockWorker
from studio.orchestration.studio import MODES, Studio
from studio.planning.validator import instantiate, list_templates
from studio.statemachine import S


def _open(d: str, defects: list[str] | None = None) -> Studio:
    path = Path(d)
    parsed = {}
    for spec in defects or []:
        parts = spec.split(":")
        parsed[parts[0]] = MockDefect(parts[1], int(parts[2]) if len(parts) > 2 else 1)
    return Studio(path, mock=MockWorker(path / "attempts", defects=parsed))


def _print_status(s: dict) -> None:
    print(f"Project {s['project_id']}  mode={s['mode']}  mock={s['mock_mode']}  done={s['done']}")
    print("States: " + ", ".join(f"{k}={v}" for k, v in sorted(s["counts"].items())))
    print(f"Model loads={s['model_loads']}  unloads={s['model_unloads']}  repairs={s['repairs']}  events={s['events']}")
    for t in s["tasks"]:
        print(f"  {t['state']:<16} a{t['attempt']}  {t['task_id']:<32} {t['capability']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shams", description="SHAMS controller CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init"); a.add_argument("dir"); a.add_argument("--project-id", required=True)
    a.add_argument("--goal", required=True); a.add_argument("--mode", choices=MODES, default="guided")
    a.add_argument("--mock", action="store_true", help="run without any AI model (Mock Worker)")
    a.add_argument("--engine", default="unity", help="unity | unreal (ue5) | godot | web (three.js/WebGPU)")
    a = sub.add_parser("plan"); a.add_argument("dir"); a.add_argument("--template", default="vertical_slice_3d")
    a.add_argument("--engine", help="override the project's engine before the first plan")
    a.add_argument("--approve", help="name of the person approving the plan")
    a = sub.add_parser("run"); a.add_argument("dir"); a.add_argument("--defect", action="append")
    a.add_argument("--max-steps", type=int, default=500)
    a = sub.add_parser("status"); a.add_argument("dir"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("review"); a.add_argument("dir")
    a = sub.add_parser("decide"); a.add_argument("dir"); a.add_argument("task_id")
    a.add_argument("decision", choices=["accept", "retry", "fail", "cancel", "unblock"]); a.add_argument("--note", default="")
    a = sub.add_parser("events"); a.add_argument("dir"); a.add_argument("--task"); a.add_argument("--since", type=int, default=0)
    a = sub.add_parser("templates"); a.add_argument("--dir")
    a = sub.add_parser("engines"); a.add_argument("--dir")
    sub.add_parser("providers")
    a = sub.add_parser("nodes"); a.add_argument("dir"); a.add_argument("--check", action="store_true")
    a = sub.add_parser("ui", help="open the SHAMS main UI (local web server)"); a.add_argument("dir")
    a.add_argument("--host", default="127.0.0.1"); a.add_argument("--port", type=int, default=8765)
    a.add_argument("--mock-delay", type=float, default=0.8, help="seconds of simulated work per mock task")
    args = p.parse_args(argv)

    if args.cmd == "ui":
        try:
            from apps.api.server import serve
        except ImportError:
            print('error: install the UI extra first: pip install -e ".[ui]"', file=sys.stderr)
            return 2
        print(f"SHAMS UI: http://{args.host}:{args.port}  (Ctrl+C to stop)")
        serve(args.dir, args.host, args.port, args.mock_delay)
        return 0

    if args.cmd == "templates":
        print("\n".join(list_templates(args.dir)))
        return 0
    if args.cmd == "providers":
        from studio import providers
        for name in providers.available():
            cls, cfg = providers.PRESETS.get(name, (None, {}))
            cfg = {"base_url": getattr(cls, "default_base_url", "") or None,
                   "api_key_env": getattr(cls, "default_key_env", None), **cfg}
            extra = " ".join(f"{k}={v}" for k, v in cfg.items() if v)
            print(f"{name:<18} {extra}")
        return 0
    if args.cmd == "engines":
        from studio import engines
        try:
            engines.load_plugins(args.dir)
        except (ValueError, KeyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for e in engines.ENGINES.values():
            print(f"{e.name:<7} {e.display:<20} {e.language:<11} {e.code_capability:<22} {e.build_target}")
        return 0
    if args.cmd == "init":
        from studio import engines
        try:
            engines.load_plugins(args.dir)
            prof = engines.get(args.engine)
        except (KeyError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        Studio.init_project(args.dir, args.project_id, args.goal, mode=args.mode, mock_mode=args.mock,
                            engine={"name": prof.name, "version": prof.default_version})
        import studio.examples
        example = Path(studio.examples.__file__).parent / "nodes.example.toml"
        if example.exists() and not (Path(args.dir) / "nodes.toml").exists():
            (Path(args.dir) / "nodes.example.toml").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Initialised {args.project_id} in {args.dir} (engine={prof.display}, mode={args.mode}, mock={args.mock})")
        if not args.mock:
            print("Next: copy nodes.example.toml to nodes.toml and declare your models/APIs (see `shams providers`).")
        return 0
    st = _open(args.dir, getattr(args, "defect", None))
    try:
        if args.cmd == "plan":
            if args.engine:
                st.set_engine(args.engine)
            issues = st.load_plan(instantiate(args.template, st.db.project_id, st.project["goal"],
                                              engine=st.project["engine"]["name"], project_dir=args.dir),
                                  approved_by=args.approve)
            for i in issues:
                print(i)
            print(f"Loaded {len(st.db.tasks())} tasks from template {args.template} "
                  f"for engine {st.project['engine']['name']}")
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
            _print_status(st.run(args.max_steps))
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
