#!/usr/bin/env python3
"""Result-driven evolution loop for scene-fuzz runs.

Reads the latest evidence per case, maintains a frontier (max load/concurrency/
duration/min buffer/overhead), and emits the next generation's directions.
The loop never invents a test result: it only reads what the runner recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

VERSION = "1.0"
MAX_LOAD = 100000
MAX_THREADS = 16
MIN_BUFFER = 4096
MAX_SOAK = 3600
MAX_CYCLES = 100
OVERHEAD_BUDGET_PCT = 20.0


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(p)


def result_line(stdout: Path):
    if not stdout.exists():
        return None
    last = None
    for line in stdout.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("RESULT "):
            try:
                last = json.loads(line[7:])
            except json.JSONDecodeError:
                pass
    return last


def parse_case_params(case: dict, run_sh: Path) -> dict:
    text = run_sh.read_text(encoding="utf-8", errors="replace") if run_sh.exists() else ""
    params = {}
    for key, pat in {
        "iters": r"--iters\s+(\d+)",
        "threads": r"--threads\s+(\d+)",
        "duration": r"--duration\s+(\d+)",
        "cycles": r"--cycles\s+(\d+)",
    }.items():
        m = re.search(pat, text)
        if m:
            params[key] = int(m.group(1))
    # Generic env parsing: any NAME containing BUFFER/DELAY with a numeric value.
    m = re.search(r"(?:^|\s)([A-Z0-9_]*(?:BUFFER)[A-Z0-9_]*)=(\d+)", text)
    if m:
        params["buffer_bytes"] = int(m.group(2))
    m = re.search(r"(?:^|\s)([A-Z0-9_]*(?:DELAY)[A-Z0-9_]*)=(\d+)", text)
    if m:
        params["consumer_delay_us"] = int(m.group(2))
    params["mode"] = (re.search(r"--mode\s+(\w+)", text).group(1) if re.search(r"--mode\s+(\w+)", text) else "")
    return params


def latest_results(run_dir: Path):
    out = []
    for cdir in sorted((run_dir / "04-cases").glob("*")):
        cp = cdir / "case.json"
        if not cp.exists():
            continue
        case = read_json(cp, {})
        cid = str(case.get("id", cdir.name))
        ev = run_dir / "05-evidence" / cid
        attempts = sorted(ev.glob("attempt-*/result.json"), key=lambda p: int(p.parent.name.split("-")[1])) if ev.exists() else []
        if not attempts:
            continue
        rp = attempts[-1]
        result = read_json(rp, {})
        metrics = result_line(rp.parent / "stdout.log") or {}
        params = parse_case_params(case, cdir / "run.sh")
        out.append({
            "case_id": cid,
            "priority": case.get("priority", ""),
            "risk": case.get("risk", ""),
            "status": result.get("status", "unknown"),
            "duration_sec": result.get("duration_sec", 0),
            "resources": result.get("resources_held", []),
            "exclusive": result.get("exclusive", False),
            "evidence": str(rp.relative_to(run_dir)),
            "metrics": metrics,
            "params": params,
        })
    return out


def frontier_from(results):
    f = {
        "max_load_kernel": 0,
        "max_threads": 0,
        "min_buffer_bytes": 0,
        "max_soak_sec": 0,
        "max_cycles": 0,
        "max_overhead_pct": 0.0,
        "max_rss_growth_kb": 0,
        "evidence": {},
    }
    for r in results:
        if r["status"] != "pass":
            continue
        m, p = r["metrics"], r["params"]
        mode = str(m.get("mode", p.get("mode", "")))
        k = int(m.get("kernel", 0) or 0)
        if mode in {"burst", "pressure", "multithread", "cycles", "fault"} and k > f["max_load_kernel"]:
            f["max_load_kernel"] = k
            f["evidence"]["max_load_kernel"] = r["case_id"]
        th = int(p.get("threads", 0) or 0)
        if th > f["max_threads"]:
            f["max_threads"] = th
            f["evidence"]["max_threads"] = r["case_id"]
        buf = int(p.get("buffer_bytes", 0) or 0)
        if buf and (f["min_buffer_bytes"] == 0 or buf < f["min_buffer_bytes"]):
            f["min_buffer_bytes"] = buf
            f["evidence"]["min_buffer_bytes"] = r["case_id"]
        dur = int(p.get("duration", 0) or 0)
        if dur > f["max_soak_sec"]:
            f["max_soak_sec"] = dur
            f["evidence"]["max_soak_sec"] = r["case_id"]
        cyc = int(p.get("cycles", 0) or 0)
        if cyc > f["max_cycles"]:
            f["max_cycles"] = cyc
            f["evidence"]["max_cycles"] = r["case_id"]
        if float(m.get("overhead_pct", 0) or 0) > f["max_overhead_pct"]:
            f["max_overhead_pct"] = round(float(m.get("overhead_pct", 0) or 0), 2)
            f["evidence"]["max_overhead_pct"] = r["case_id"]
        growth = int(m.get("rss_kb", 0) or 0) - int(m.get("rss_start_kb", 0) or 0)
        if growth > f["max_rss_growth_kb"]:
            f["max_rss_growth_kb"] = growth
            f["evidence"]["max_rss_growth_kb"] = r["case_id"]
    return f


def signals_from(results):
    failures = [r["case_id"] for r in results if r["status"] in {"fail", "timeout", "signal", "setup_failed", "error"}]
    drops = max([int(r["metrics"].get("dropped", 0) or 0) for r in results] or [0])
    host_fail = [r["case_id"] for r in results if r["metrics"].get("host_ok") is False]
    env_blocked = [r["case_id"] for r in results if r["status"] == "env-blocked"]
    return {
        "failures": failures,
        "drops": drops,
        "host_fail": host_fail,
        "env_blocked": env_blocked,
        "max_overhead_pct": max([float(r["metrics"].get("overhead_pct", 0) or 0) for r in results] or [0.0]),
    }


def recommend(state, results):
    if state.get("stop"):
        return []
    f, s = state["frontier"], state["signals"]
    known = set(state.get("known_failures", []))
    recs = []
    unknown = [c for c in s["failures"] if c not in known]
    if unknown:
        recs.append({
            "axis": "minimize-failure", "priority": "P0",
            "why": f"未归因失败 {unknown[0]}：先最小化/复现，再决定是否产品缺陷",
            "params": {"case": unknown[0]}, "exclusive": True,
        })
        return recs
    if s["host_fail"]:
        recs.append({
            "axis": "stability-isolate", "priority": "P0",
            "why": f"宿主任务失败 {s['host_fail'][0]}：降载/单线程/超时隔离，优先稳定性",
            "params": {"case": s["host_fail"][0]}, "exclusive": True,
        })
        return recs
    if s["drops"] > 0:
        recs.append({
            "axis": "pressure-threshold", "priority": "P0",
            "why": f"已出现 dropped={s['drops']}：减半 buffer、增加消费者延迟、×2 负载，找丢数阈值",
            "params": {"buffer_divisor": 2, "load_multiplier": 2}, "exclusive": True,
        })
    if f["max_threads"] < MAX_THREADS:
        recs.append({
            "axis": "concurrency", "priority": "P0",
            "why": f"当前并发前沿 {f['max_threads']} threads：+4，验证竞态/计数/生命周期",
            "params": {"threads": min(MAX_THREADS, f["max_threads"] + 4)},
            "resources": ["npu:0"], "exclusive": True,
        })
    if f["min_buffer_bytes"] > MIN_BUFFER:
        nb = max(MIN_BUFFER, f["min_buffer_bytes"] // 2)
        recs.append({
            "axis": "pressure", "priority": "P1",
            "why": f"当前最小 buffer {f['min_buffer_bytes']}B：减半到 {nb}B，配合慢消费者",
            "params": {"buffer_bytes": nb, "consumer_delay_us": 100000},
            "resources": ["npu:0"], "exclusive": True,
        })
    if f["max_soak_sec"] < MAX_SOAK:
        ns = min(MAX_SOAK, max(300, f["max_soak_sec"] * 2))
        recs.append({
            "axis": "long-stability", "priority": "P0",
            "why": f"当前长稳前沿 {f['max_soak_sec']}s：延长到 {ns}s，观察 RSS/fd/线程/吞吐漂移",
            "params": {"duration": ns}, "resources": ["npu:0"], "exclusive": True,
        })
    if s["max_overhead_pct"] > OVERHEAD_BUDGET_PCT:
        recs.append({
            "axis": "overhead-attribution", "priority": "P1",
            "why": f"最大开销 {s['max_overhead_pct']:.1f}% > {OVERHEAD_BUDGET_PCT}%：按 kind/回调/flush 拆分，确认是否负载相关",
            "params": {"baseline": "off", "split_by": ["kind", "flush_period", "consumer_delay"]},
        })
    if f["max_load_kernel"] < MAX_LOAD:
        nl = min(MAX_LOAD, max(10000, f["max_load_kernel"] * 10))
        recs.append({
            "axis": "load", "priority": "P1",
            "why": f"当前负载前沿 {f['max_load_kernel']} kernels：×10 到 {nl}",
            "params": {"iters": nl}, "resources": ["npu:0"], "exclusive": True,
        })
    if not recs:
        recs = [{
            "axis": "change-axis", "priority": "P1",
            "why": "主要轴已接近上限：转向故障组合/ABI/差分/多进程，或停止并写 loop-report",
            "params": {"directions": ["fault-combination", "abi-matrix", "differential", "multi-process"]},
        }]
    return recs[:4]


def render_frontier(state) -> str:
    f, s = state["frontier"], state["signals"]
    lines = [
        "# 演进前沿",
        "",
        f"- iteration: {state['iteration']}",
        f"- updated: {state.get('updated_at','')}",
        f"- stop: {state.get('stop')} {state.get('stop_reason','')}",
        "",
        "| 轴 | 当前前沿 | 证据 |",
        "|---|---|---|",
        f"| load | {f['max_load_kernel']} kernels | {f['evidence'].get('max_load_kernel','-')} |",
        f"| concurrency | {f['max_threads']} threads | {f['evidence'].get('max_threads','-')} |",
        f"| pressure | {f['min_buffer_bytes']} B buffer | {f['evidence'].get('min_buffer_bytes','-')} |",
        f"| long-stability | {f['max_soak_sec']} s | {f['evidence'].get('max_soak_sec','-')} |",
        f"| lifecycle | {f['max_cycles']} cycles | {f['evidence'].get('max_cycles','-')} |",
        f"| overhead | {f['max_overhead_pct']}% | {f['evidence'].get('max_overhead_pct','-')} |",
        f"| rss growth | {f['max_rss_growth_kb']} KB | {f['evidence'].get('max_rss_growth_kb','-')} |",
        "",
        "## Signals",
        "",
        f"- failures: {', '.join(s['failures']) or '-'}",
        f"- host_fail: {', '.join(s['host_fail']) or '-'}",
        f"- drops: {s['drops']}",
        f"- env_blocked: {', '.join(s['env_blocked']) or '-'}",
        "",
        "## Next",
        "",
    ]
    for r in state.get("next", []):
        lines.append(f"- **{r['axis']}** ({r['priority']}): {r['why']} params={r.get('params', {})}")
    lines.append("")
    return "\n".join(lines)


def load_state(run_dir: Path):
    p = run_dir / "09-loop" / "state.json"
    if not p.exists():
        raise SystemExit(f"loop not initialized: {p}")
    return read_json(p, {})


def save_state(run_dir: Path, state) -> None:
    loop = run_dir / "09-loop"
    loop.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now()
    write_json(loop / "state.json", state)
    (loop / "frontier.md").write_text(render_frontier(state), encoding="utf-8")


def append_iteration(run_dir: Path, entry) -> None:
    p = run_dir / "09-loop" / "iterations.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def cmd_init(args):
    run_dir = Path(args.run_dir).resolve()
    loop = run_dir / "09-loop"
    if (loop / "state.json").exists() and not args.force:
        raise SystemExit("loop already initialized (use --force)")
    loop.mkdir(parents=True, exist_ok=True)
    results = latest_results(run_dir)
    if not results:
        raise SystemExit("no evidence found; run cases first")
    state = {
        "version": VERSION,
        "run_id": read_json(run_dir / "run.json", {}).get("run_id", ""),
        "created_at": now(),
        "updated_at": now(),
        "iteration": 1,
        "budget": {"max_iterations": args.max_iterations, "max_wall_sec": args.max_wall_sec},
        "known_failures": [x.strip() for x in (args.known_failures or "").split(",") if x.strip()],
        "frontier": frontier_from(results),
        "signals": signals_from(results),
        "observations": [{"case_id": r["case_id"], "status": r["status"], "evidence": r["evidence"]} for r in results],
        "next": [],
        "stop": False,
        "stop_reason": "",
        "history": [],
    }
    state["next"] = recommend(state, results)
    save_state(run_dir, state)
    append_iteration(run_dir, {
        "iteration": 1, "kind": "baseline", "cases": len(results),
        "findings": state["signals"]["failures"], "next": [r["axis"] for r in state["next"]],
        "ts": now(),
    })
    print(f"loop initialized: {loop}")
    print(f"iteration=1 cases={len(results)} next={[r['axis'] for r in state['next']]}")


def cmd_next(args):
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    if args.json:
        print(json.dumps(state.get("next", []), ensure_ascii=False, indent=2))
        return 0
    if state.get("stop"):
        print(f"STOP: {state.get('stop_reason')}")
        return 0
    for i, r in enumerate(state.get("next", []), 1):
        print(f"{i}. [{r['priority']}] {r['axis']}: {r['why']}")
        if r.get("params"):
            print(f"   params: {json.dumps(r['params'], ensure_ascii=False)}")
    if not state.get("next"):
        print("no next recommendation (check budget/stop)")
    return 0


def cmd_status(args):
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    print(f"iteration={state['iteration']} stop={state['stop']} reason={state.get('stop_reason','')}")
    print("frontier:", json.dumps(state["frontier"], ensure_ascii=False))
    print("signals:", json.dumps(state["signals"], ensure_ascii=False))
    print("next:", [r["axis"] for r in state.get("next", [])])
    return 0


def cmd_record(args):
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    results = latest_results(run_dir)
    hit = next((r for r in results if r["case_id"] == args.case), None)
    if not hit:
        raise SystemExit(f"no evidence for case {args.case}")
    iteration = args.iteration or (state["iteration"] + 1)
    old_frontier = json.dumps(state["frontier"], sort_keys=True)
    state["frontier"] = frontier_from(results)
    state["signals"] = signals_from(results)
    state["iteration"] = iteration
    state["observations"].append({"case_id": hit["case_id"], "status": hit["status"], "evidence": hit["evidence"]})
    state["next"] = recommend(state, results)
    if iteration >= state["budget"]["max_iterations"]:
        state["stop"] = True
        state["stop_reason"] = "max_iterations"
    if json.dumps(state["frontier"], sort_keys=True) == old_frontier:
        state["no_signal_count"] = state.get("no_signal_count", 0) + 1
    else:
        state["no_signal_count"] = 0
    if state.get("no_signal_count", 0) >= 3 and not state["stop"]:
        state["next"] = [{
            "axis": "change-axis", "priority": "P1",
            "why": "连续 3 代前沿无变化：换轴或停止，避免重复同一负载",
            "params": {"directions": ["fault", "abi", "differential", "multi-process"]},
        }]
    save_state(run_dir, state)
    append_iteration(run_dir, {
        "iteration": iteration, "kind": "generation", "case": hit["case_id"],
        "axis": args.axis, "change": args.change,
        "outcome": hit["status"], "metrics": hit["metrics"], "params": hit["params"],
        "evidence": hit["evidence"], "next": [r["axis"] for r in state["next"]], "ts": now(),
    })
    print(f"recorded iteration={iteration} case={hit['case_id']} outcome={hit['status']}")
    print("next:", [r["axis"] for r in state["next"]])


def cmd_frontier(args):
    run_dir = Path(args.run_dir).resolve()
    state = load_state(run_dir)
    print(render_frontier(state))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evolve.py", description="scene-fuzz evolution loop")
    ap.add_argument("--version", action="version", version=f"evolve {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--known-failures", default="")
    p.add_argument("--max-iterations", type=int, default=10)
    p.add_argument("--max-wall-sec", type=int, default=7200)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("next")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("status")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("record")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--axis", default="unspecified")
    p.add_argument("--change", default="")
    p.add_argument("--iteration", type=int, default=0)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("frontier")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_frontier)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
