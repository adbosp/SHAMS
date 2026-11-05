"""`shams` CLI (ADR-112): human review and operation before the desktop UI exists.

  shams init <dir> --project-id shark_mvp --goal "..." [--mode guided] [--mock]
  shams plan <dir> --template unity_3d_vertical_slice --approve <name>
  shams run <dir> [--defect task_id:criterion_id[:fail_attempts]]...
  shams status <dir> [--json]
  shams review <dir>                      # list tasks waiting for a human
  shams decide <dir> <task_id> accept|retry|fail|cancel|unblock [--note ...]
  shams events <dir> [--task <id>] [--since N]
  shams templates
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
    a = sub.add_parser("plan"); a.add_argument("dir"); a.add_argument("--template", required=True)
    a.add_argument("--approve", help="name of the person approving the plan")
    a = sub.add_parser("run"); a.add_argument("dir"); a.add_argument("--defect", action="append")
    a.add_argument("--max-steps", type=int, default=500)
    a = sub.add_parser("status"); a.add_argument("dir"); a.add_argument("--json", action="store_true")
    a = sub.add_parser("review"); a.add_argument("dir")
    a = sub.add_parser("decide"); a.add_argument("dir"); a.add_argument("task_id")
    a.add_argument("decision", choices=["accept", "retry", "fail", "cancel", "unblock"]); a.add_argument("--note", default="")
    a = sub.add_parser("events"); a.add_argument("dir"); a.add_argument("--task"); a.add_argument("--since", type=int, default=0)
    sub.add_parser("templates")
    args = p.parse_args(argv)

    if args.cmd == "templates":
        print("\n".join(list_templates()))
        return 0
    if args.cmd == "init":
        Studio.init_project(args.dir, args.project_id, args.goal, mode=args.mode, mock_mode=args.mock)
        print(f"Initialised {args.project_id} in {args.dir} (mode={args.mode}, mock={args.mock})")
        return 0
    st = _open(args.dir, getattr(args, "defect", None))
    try:
        if args.cmd == "plan":
            issues = st.load_plan(instantiate(args.template, st.db.project_id, st.project["goal"]),
                                  approved_by=args.approve)
            for i in issues:
                print(i)
            print(f"Loaded {len(st.db.tasks())} tasks from template {args.template}")
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
    except (ValueError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
