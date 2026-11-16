#!/usr/bin/env python3
"""scene-fuzz runner: run layout, probing, case execution, parallel suites, evidence gate.

Standard library only. Linux oriented (uses /proc, process groups, flock).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Linux
    fcntl = None

SKILL_VERSION = "1.0"
CONTEXT_FILES = [
    "repo-map.md",
    "contracts.md",
    "usage-context.md",
    "usage-scenarios.md",
    "history-review.md",
    "risk-model.md",
    "tooling.md",
    "domain-kg.md",
]
REPORT_HEADINGS = [f"## {i}." for i in range(1, 14)]
TOOL_CANDIDATES = [
    "cc", "c++", "gcc", "g++", "cmake", "make", "ninja", "bazel", "meson",
    "clang", "clang++", "clangd", "clang-tidy", "cppcheck", "scan-build",
    "infer", "semgrep", "codeql", "ctags", "cscope", "gtags", "tree-sitter",
    "gdb", "lldb", "valgrind", "perf", "strace", "ltrace", "gcov", "lcov",
    "llvm-cov", "nm", "objdump", "readelf", "ldd", "strings", "file",
    "bear", "compiledb", "doxygen", "sphinx-build", "mkdocs", "shellcheck",
    "ruff", "mypy", "pyright", "pylint", "pytest", "hypothesis", "afl-fuzz",
    "honggfuzz", "cargo", "rustc", "go", "java", "javac", "mvn", "gradle",
    "node", "npm", "pnpm", "yarn", "curl", "wget", "jq", "git", "python3",
    "npu-smi", "nvidia-smi",
]
SECRET_PATTERNS = [
    re.compile(r"kg-[a-z]+-[0-9a-f]{20,}"),
    re.compile(r"ASCEND_KG_API_KEY\s*=\s*['\"]?[A-Za-z0-9_-]{8,}"),
]
SECRET_KEY_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|PRIVATE_KEY)", re.I)

_ATTEMPT_LOCK = threading.Lock()
_JSON_LOCK = threading.Lock()


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def now_epoch() -> float:
    return time.time()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default=None):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def run_cmd(cmd, cwd=None, timeout=10, env=None):
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "not found"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def append_ledger(run_dir: Path, entry: dict) -> None:
    path = run_dir / "ledger.jsonl"
    with _JSON_LOCK:
        with path.open("a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def resolve_placeholders(text: str, run_dir: Path, case_dir: Path, evidence_dir: Path) -> str:
    if not isinstance(text, str):
        return text
    return (text.replace("{run}", str(run_dir))
                .replace("{case}", str(case_dir))
                .replace("{evidence}", str(evidence_dir)))


def _case_path(run_dir: Path, case_arg: str) -> Path:
    p = Path(case_arg)
    if p.is_absolute():
        return p
    cand = (Path.cwd() / p)
    if cand.exists():
        return cand
    return run_dir / p


def _load_case(run_dir: Path, case_arg: str) -> tuple[Path, dict]:
    path = _case_path(run_dir, case_arg)
    data = read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"case not found or invalid JSON: {path}")
    cid = str(data.get("id", "")).strip()
    if not cid:
        raise SystemExit(f"case missing id: {path}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid):
        raise SystemExit(f"invalid case id: {cid}")
    data["_path"] = str(path)
    return path, data


def _next_attempt(evidence_root: Path) -> int:
    with _ATTEMPT_LOCK:
        evidence_root.mkdir(parents=True, exist_ok=True)
        nums = []
        for p in evidence_root.glob("attempt-*"):
            try:
                nums.append(int(p.name.split("-", 1)[1]))
            except Exception:
                pass
        n = max(nums) + 1 if nums else 1
        (evidence_root / f"attempt-{n}").mkdir(parents=True, exist_ok=False)
        return n


class _Monitor(threading.Thread):
    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak_rss_kb = 0
        self.peak_threads = 0
        self.stop = threading.Event()

    def run(self):
        status = Path(f"/proc/{self.pid}/status")
        while not self.stop.is_set():
            try:
                text = status.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("VmHWM:"):
                        self.peak_rss_kb = max(self.peak_rss_kb, int(line.split()[1]))
                    elif line.startswith("Threads:"):
                        self.peak_threads = max(self.peak_threads, int(line.split()[1]))
            except Exception:
                pass
            self.stop.wait(0.1)


def _kill_group(pid: int, sig) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except Exception:
        try:
            os.kill(pid, sig)
        except Exception:
            pass


def _truncate(path: Path, max_mb: int) -> None:
    try:
        if max_mb > 0 and path.exists() and path.stat().st_size > max_mb * 1024 * 1024:
            with path.open("ab") as f:
                f.write(f"\n[truncated by sf.py: > {max_mb} MB]\n".encode())
    except Exception:
        pass


def run_case(run_dir: Path, case_arg: str, repeat: int | None = None,
             concurrent_with=None, resources=None, exclusive=False) -> dict:
    run_dir = run_dir.resolve()
    case_path, case = _load_case(run_dir, case_arg)
    cid = case["id"]
    case_dir = case_path.parent
    if repeat is None:
        repeat = int(case.get("repeat", 1))
    repeat = max(1, int(repeat))
    resources = list(resources if resources is not None else case.get("resources", []) or [])
    exclusive = bool(exclusive or case.get("exclusive", False))
    concurrent_with = list(concurrent_with or [])
    results = []
    for _ in range(repeat):
        attempt = _next_attempt(run_dir / "05-evidence" / cid)
        ev = run_dir / "05-evidence" / cid / f"attempt-{attempt}"
        (ev / "artifacts").mkdir(parents=True, exist_ok=True)
        stdout_path = ev / "stdout.log"
        stderr_path = ev / "stderr.log"
        setup_stdout = ev / "setup.stdout.log"
        setup_stderr = ev / "setup.stderr.log"
        env = os.environ.copy()
        for k, v in (case.get("env") or {}).items():
            env[str(k)] = str(v)
        env["SCENE_FUZZ_RUN_DIR"] = str(run_dir)
        env["SCENE_FUZZ_CASE_DIR"] = str(case_dir)
        env["SCENE_FUZZ_EVIDENCE_DIR"] = str(ev)
        env["SCENE_FUZZ_CASE_ID"] = cid
        env.setdefault("PYTHONUNBUFFERED", "1")

        setup = case.get("setup") or {}
        run = case.get("run") or {}
        cwd_raw = run.get("cwd") or "."
        cwd = Path(resolve_placeholders(str(cwd_raw), run_dir, case_dir, ev))
        if not cwd.is_absolute():
            cwd = (case_dir / cwd).resolve()
        timeout_sec = float(run.get("timeout_sec") or case.get("timeout_sec") or 120)
        max_log_mb = int(case.get("max_log_mb", 100))

        started_at = now_iso()
        started_epoch = now_epoch()
        setup_rc = 0
        if setup.get("cmd"):
            scmd = resolve_placeholders(setup["cmd"], run_dir, case_dir, ev)
            scwd = Path(resolve_placeholders(str(setup.get("cwd") or cwd), run_dir, case_dir, ev))
            if not scwd.is_absolute():
                scwd = (case_dir / scwd).resolve()
            stimeout = float(setup.get("timeout_sec") or 120)
            with setup_stdout.open("w", encoding="utf-8", errors="replace") as so, \
                    setup_stderr.open("w", encoding="utf-8", errors="replace") as se:
                try:
                    p = subprocess.run(scmd, shell=isinstance(scmd, str), cwd=scwd, env=env,
                                       stdout=so, stderr=se, timeout=stimeout,
                                       start_new_session=True)
                    setup_rc = p.returncode
                except subprocess.TimeoutExpired:
                    setup_rc = 124
                except Exception as exc:  # noqa: BLE001
                    setup_rc = 1
                    se.write(str(exc))
            _truncate(setup_stdout, max_log_mb)
            _truncate(setup_stderr, max_log_mb)

        exit_code = None
        sig = None
        timed_out = False
        status = "fail"
        duration = 0.0
        peak_rss = 0
        peak_threads = 0
        monitor = None
        if setup_rc != 0:
            status = "setup_failed"
            exit_code = setup_rc
        else:
            cmd = resolve_placeholders(run.get("cmd", ""), run_dir, case_dir, ev)
            if not cmd:
                status = "error"
                exit_code = 2
            else:
                with stdout_path.open("wb") as so, stderr_path.open("wb") as se:
                    t0 = time.time()
                    try:
                        p = subprocess.Popen(
                            cmd, shell=isinstance(cmd, str), cwd=cwd, env=env,
                            stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                        monitor = _Monitor(p.pid)
                        monitor.start()
                        try:
                            p.wait(timeout=timeout_sec)
                            exit_code = p.returncode
                        except subprocess.TimeoutExpired:
                            timed_out = True
                            _kill_group(p.pid, signal.SIGTERM)
                            try:
                                p.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                _kill_group(p.pid, signal.SIGKILL)
                                try:
                                    p.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    pass
                            exit_code = p.returncode
                        if exit_code is not None and exit_code < 0:
                            sig = -exit_code
                            exit_code = None
                    except Exception as exc:  # noqa: BLE001
                        exit_code = 2
                        se.write(str(exc).encode("utf-8", "replace"))
                    finally:
                        duration = time.time() - t0
                        if monitor:
                            monitor.stop.set()
                            monitor.join(timeout=1)
                            peak_rss = monitor.peak_rss_kb
                            peak_threads = monitor.peak_threads
                            monitor = None
                _truncate(stdout_path, max_log_mb)
                _truncate(stderr_path, max_log_mb)
                if timed_out:
                    status = "timeout"
                elif sig is not None:
                    status = "signal"
                elif exit_code == 0:
                    status = "pass"
                else:
                    status = "fail"

        finished_at = now_iso()
        finished_epoch = now_epoch()
        result = {
            "case_id": cid,
            "attempt": attempt,
            "title": case.get("title", ""),
            "priority": case.get("priority", ""),
            "risk": case.get("risk", ""),
            "cmd": run.get("cmd", ""),
            "cwd": str(cwd),
            "setup_rc": setup_rc,
            "exit_code": exit_code,
            "signal": sig,
            "timed_out": timed_out,
            "status": status,
            "duration_sec": round(duration, 3),
            "peak_rss_kb": peak_rss,
            "threads_peak": peak_threads,
            "stdout_path": str(stdout_path.relative_to(run_dir)),
            "stderr_path": str(stderr_path.relative_to(run_dir)),
            "setup_stdout_path": str(setup_stdout.relative_to(run_dir)),
            "setup_stderr_path": str(setup_stderr.relative_to(run_dir)),
            "started_at": started_at,
            "finished_at": finished_at,
            "started_epoch": started_epoch,
            "finished_epoch": finished_epoch,
            "sha256_cmd": sha256_text(str(run.get("cmd", ""))),
            "env_keys": sorted((case.get("env") or {}).keys()),
            "resources_held": resources,
            "exclusive": exclusive,
            "concurrent_with": concurrent_with,
        }
        write_json(ev / "result.json", result)
        append_ledger(run_dir, {
            "ts": finished_at, "case_id": cid, "attempt": attempt,
            "status": status, "exit_code": exit_code, "signal": sig,
            "timed_out": timed_out, "duration_sec": result["duration_sec"],
            "resources_held": resources, "exclusive": exclusive,
            "concurrent_with": concurrent_with,
            "evidence": str((ev / "result.json").relative_to(run_dir)),
        })
        results.append(result)
        print(f"[{cid}] attempt {attempt}: {status} (exit={exit_code}, sig={sig}, {result['duration_sec']}s)")
    rc = 0 if results and all(r["status"] == "pass" for r in results) else 1
    return {"case_id": cid, "rc": rc, "results": results}


def _load_case_list(run_dir: Path, case_list: str | None, priority: str | None):
    items = []
    if case_list:
        raw = read_json(run_dir / case_list) if not Path(case_list).is_absolute() else read_json(Path(case_list))
        if isinstance(raw, dict):
            raw = raw.get("cases", [])
        if not isinstance(raw, list):
            raise SystemExit("case-list must be a list or {cases: [...]}")
        for it in raw:
            if isinstance(it, str):
                items.append({"id": it})
            elif isinstance(it, dict):
                items.append(dict(it))
    else:
        for p in sorted((run_dir / "04-cases").glob("*/case.json")):
            data = read_json(p, {})
            items.append({"id": data.get("id", p.parent.name), "path": str(p)})
    out = []
    for it in items:
        cid = str(it.get("id", "")).strip()
        if not cid:
            continue
        path = it.get("path") or f"04-cases/{cid}/case.json"
        p = Path(path)
        if not p.is_absolute():
            p = run_dir / p
        data = read_json(p, {})
        if not data:
            raise SystemExit(f"case missing: {p}")
        if priority:
            wanted = {x.strip() for x in priority.split(",") if x.strip()}
            if str(data.get("priority", "")) not in wanted:
                continue
        out.append({
            "id": cid,
            "path": str(p),
            "priority": data.get("priority", ""),
            "resources": list(it.get("resources", data.get("resources", []) or [])),
            "exclusive": bool(it.get("exclusive", data.get("exclusive", False))),
            "repeat": it.get("repeat", data.get("repeat", 1)),
        })
    return out


def run_suite(run_dir: Path, case_list: str | None, jobs: int, priority: str | None, dry_run: bool) -> int:
    run_dir = run_dir.resolve()
    cases = _load_case_list(run_dir, case_list, priority)
    if not cases:
        print("no cases selected")
        return 1
    if dry_run:
        for c in cases:
            print(f"{c['priority']:>3} {c['id']:<24} resources={c['resources']} exclusive={c['exclusive']}")
        return 0
    jobs = max(1, int(jobs))
    lock = threading.Lock()
    pending = list(cases)
    running = {}
    held = set()
    global_exclusive = False
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        while pending or running:
            started = False
            with lock:
                if not global_exclusive:
                    for task in list(pending):
                        task_global = bool(task["exclusive"] and not task["resources"])
                        if task_global and running:
                            continue
                        if any(bool(t["exclusive"] and not t["resources"]) for t in running.values()):
                            continue
                        if not set(task["resources"]).isdisjoint(held):
                            continue
                        active = [t["id"] for t in running.values()]
                        fut = ex.submit(run_case, run_dir, task["path"], task["repeat"],
                                        active, task["resources"], task["exclusive"])
                        running[fut] = task
                        held.update(task["resources"])
                        if task_global:
                            global_exclusive = True
                        pending.remove(task)
                        started = True
                        break
            if started:
                continue
            if not running:
                # Defensive: start the first pending even if resources look conflicting.
                task = pending.pop(0)
                fut = ex.submit(run_case, run_dir, task["path"], task["repeat"], [], task["resources"], task["exclusive"])
                running[fut] = task
                held.update(task["resources"])
                if task["exclusive"] and not task["resources"]:
                    global_exclusive = True
                continue
            done, _ = concurrent.futures.wait(running.keys(), return_when=concurrent.futures.FIRST_COMPLETED, timeout=0.5)
            for fut in done:
                task = running.pop(fut)
                held.difference_update(task["resources"])
                if task["exclusive"] and not task["resources"]:
                    global_exclusive = False
                try:
                    res = fut.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[{task['id']}] suite error: {exc}")
                    results.append({"case_id": task["id"], "rc": 1, "error": str(exc)})
                    continue
                results.append(res)
    failed = [r for r in results if r.get("rc") != 0]
    print(f"suite: {len(results)} cases, {len(failed)} failed")
    return 0 if not failed else 1


def _cmd_init(args) -> int:
    target = Path(args.target).resolve()
    run_dir = Path(args.run_dir).resolve()
    if not target.exists():
        raise SystemExit(f"target does not exist: {target}")
    if run_dir.exists() and any(run_dir.iterdir()) and not args.force:
        raise SystemExit(f"run dir not empty: {run_dir} (use --force)")
    run_dir.mkdir(parents=True, exist_ok=True)
    for d in ["00-run-meta", "01-context", "02-scenarios", "03-harness/src", "04-cases",
              "05-evidence", "06-analysis/findings", "07-report", "08-retrospective", "locks"]:
        (run_dir / d).mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"{target.name}-{_dt.date.today().strftime('%Y%m%d')}-unknown"
    run_json = run_dir / "run.json"
    if not run_json.exists() or args.force:
        write_json(run_json, {
            "run_id": run_id,
            "target": str(target),
            "target_abs": str(target),
            "created_at": now_iso(),
            "skill_version": SKILL_VERSION,
            "skill_dir": str(Path(__file__).resolve().parent.parent),
        })
    (run_dir / "ledger.jsonl").touch(exist_ok=True)
    print(f"initialized {run_dir}")
    print(f"target: {target}")
    print("next: python3 sf.py probe --run-dir", run_dir)
    return 0


def _version_of(tool: str, path: str) -> str:
    rc, out, err = run_cmd([path, "--version"], timeout=2)
    text = (out or err).strip().splitlines()
    if rc == 0 and text:
        return text[0][:240]
    rc, out, err = run_cmd([path, "-version"], timeout=2)
    text = (out or err).strip().splitlines()
    return text[0][:240] if text else ""


def _cmd_probe(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    run_json = read_json(run_dir / "run.json", {})
    if not run_json:
        raise SystemExit(f"no run.json in {run_dir}; run init first")
    target = Path(run_json["target_abs"])
    meta = run_dir / "00-run-meta"
    meta.mkdir(parents=True, exist_ok=True)

    uname = platform.uname()
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
                break
    except Exception:
        pass
    host = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "system": uname.system,
        "release": uname.release,
        "cpu_count": os.cpu_count(),
        "mem_total_kb": mem_kb,
        "python": sys.version,
        "cwd": str(Path.cwd()),
        "probed_at": now_iso(),
    }
    write_json(meta / "host.json", host)

    git = {"is_git": False}
    if (target / ".git").exists() or (target / ".git").is_file():
        git["is_git"] = True
        for key, cmd in {
            "commit": ["git", "-C", str(target), "rev-parse", "HEAD"],
            "branch": ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
            "status": ["git", "-C", str(target), "status", "--porcelain"],
            "log": ["git", "-C", str(target), "log", "-1", "--format=%H %ci %s"],
            "diff_stat": ["git", "-C", str(target), "diff", "--stat"],
        }.items():
            rc, out, err = run_cmd(cmd, timeout=15)
            git[key] = out.strip() if rc == 0 else (err.strip() or f"rc={rc}")
    write_json(meta / "git.json", git)

    tools = {}
    for name in TOOL_CANDIDATES:
        path = shutil.which(name)
        rec = {"available": bool(path), "path": path or "", "version": ""}
        if path:
            rec["version"] = _version_of(name, path)
        tools[name] = rec
    write_json(meta / "tools.json", tools)

    resources = {"cpu_count": os.cpu_count(), "mem_total_kb": mem_kb}
    for name, cmd in {"npu-smi": ["npu-smi", "info"], "nvidia-smi": ["nvidia-smi", "-L"]}.items():
        if shutil.which(name):
            rc, out, err = run_cmd(cmd, timeout=10)
            resources[name] = {"rc": rc, "text": (out or err)[:8000]}
    write_json(meta / "resources.json", resources)

    keys = ["PATH", "LD_LIBRARY_PATH", "ASCEND_HOME_PATH", "ASCEND_RT_VISIBLE_DEVICES",
            "DSH_HOME", "CUDA_VISIBLE_DEVICES", "VIRTUAL_ENV", "CONDA_PREFIX"]
    env_info = {}
    for k in keys:
        if k in os.environ:
            env_info[k] = os.environ[k] if not SECRET_KEY_RE.search(k) else "<redacted>"
    api_key = os.environ.get("ASCEND_KG_API_KEY", "")
    env_info["ASCEND_KG_API_KEY"] = {
        "present": bool(api_key),
        "fingerprint": hashlib.sha256(api_key.encode()).hexdigest()[:12] if api_key else "",
    }
    write_json(meta / "env.json", env_info)

    hashes = {"target": str(target), "libs": {}}
    src_fp = ""
    if git.get("is_git"):
        src_fp = sha256_text(str(git.get("commit")) + "\n" + str(git.get("status")) + "\n" + str(git.get("diff_stat")))
    else:
        h = hashlib.sha256()
        count = 0
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in {".git", "build", "node_modules", "__pycache__", ".cache"}]
            for fn in sorted(files):
                p = Path(root) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                h.update(f"{p.relative_to(target)}|{st.st_size}\n".encode())
                count += 1
                if count > 50000:
                    break
            if count > 50000:
                break
        src_fp = h.hexdigest()
    hashes["source_fingerprint"] = src_fp
    for p in sorted(target.rglob("lib*.so")):
        if "build" in p.parts or "prefix" in p.parts:
            try:
                hashes["libs"][str(p.relative_to(target))] = sha256_file(p)
            except Exception:
                pass
        if len(hashes["libs"]) >= 20:
            break
    write_json(meta / "hashes.json", hashes)
    print(f"probed: {meta}")
    return 0


def _cmd_run(args) -> int:
    res = run_case(Path(args.run_dir), args.case, args.repeat)
    return res["rc"]


def _parse_frontmatter(text: str):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    block = text[3:end].strip().splitlines()
    data = {}
    current_list = None
    for line in block:
        raw = line.rstrip()
        if not raw.strip():
            continue
        if raw.lstrip().startswith("- ") and current_list is not None:
            data[current_list].append(raw.lstrip()[2:].strip().strip("'\""))
            continue
        if ":" in raw:
            key, val = raw.split(":", 1)
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                data[key] = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
                current_list = None
            elif val == "":
                data[key] = []
                current_list = key
            else:
                data[key] = val
                current_list = None
    return data


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
        return b"\x00" not in chunk
    except Exception:
        return False


def _cmd_check(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    errors = []
    warnings = []
    run_json = read_json(run_dir / "run.json", {})
    if not run_json:
        errors.append("missing run.json")
    for name in CONTEXT_FILES:
        p = run_dir / "01-context" / name
        if not p.exists() or p.stat().st_size < 20:
            errors.append(f"missing/too small context: 01-context/{name}")
    report = run_dir / "REPORT.md"
    if not report.exists() or report.stat().st_size < 100:
        errors.append("missing/too small REPORT.md")
    else:
        text = report.read_text(encoding="utf-8", errors="replace")
        for h in REPORT_HEADINGS:
            if h not in text:
                errors.append(f"REPORT.md missing heading {h}")
    case_list_path = run_dir / "02-scenarios" / "case-list.json"
    cases = []
    if case_list_path.exists():
        raw = read_json(case_list_path, {})
        if isinstance(raw, dict):
            raw = raw.get("cases", [])
        for it in raw or []:
            if isinstance(it, str):
                cases.append({"id": it, "priority": "P0"})
            elif isinstance(it, dict):
                cases.append(it)
    else:
        for p in sorted((run_dir / "04-cases").glob("*/case.json")):
            data = read_json(p, {})
            cases.append({"id": data.get("id", p.parent.name), "priority": data.get("priority", "P0")})
    for c in cases:
        if str(c.get("priority", "")) != "P0":
            continue
        cid = str(c.get("id", ""))
        ev = run_dir / "05-evidence" / cid
        attempts = list(ev.glob("attempt-*/result.json")) if ev.exists() else []
        if not attempts:
            errors.append(f"P0 case has no evidence: {cid}")
    findings = sorted((run_dir / "06-analysis" / "findings").glob("*.md")) if (run_dir / "06-analysis" / "findings").exists() else []
    for f in findings:
        fm = _parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        if not fm:
            errors.append(f"finding missing frontmatter: {f.name}")
            continue
        for key in ["id", "severity", "status", "evidence"]:
            if key not in fm:
                errors.append(f"finding {f.name} missing {key}")
        if fm.get("severity") not in {"S0", "S1", "S2", "S3"}:
            errors.append(f"finding {f.name} invalid severity: {fm.get('severity')}")
        if fm.get("status") not in {"confirmed", "suspected", "env-blocked", "not-a-bug"}:
            errors.append(f"finding {f.name} invalid status: {fm.get('status')}")
        evs = fm.get("evidence") or []
        if not isinstance(evs, list):
            evs = [evs]
        for e in evs:
            p = (run_dir / str(e)).resolve()
            if not str(p).startswith(str(run_dir) + os.sep):
                errors.append(f"finding {f.name} evidence escapes run dir: {e}")
            elif not p.exists():
                errors.append(f"finding {f.name} evidence missing: {e}")
    results = []
    latest = {}
    for rp in (run_dir / "05-evidence").glob("*/attempt-*/result.json"):
        r = read_json(rp)
        if not isinstance(r, dict):
            continue
        cid = str(r.get("case_id", ""))
        attempt = int(r.get("attempt", 0) or 0)
        if cid not in latest or attempt > latest[cid][1]:
            latest[cid] = (rp, attempt, r)
    results = [(rp, r) for (rp, _attempt, r) in latest.values()]
    for i in range(len(results)):
        for j in range(i + 1, len(results)):
            pa, a = results[i]
            pb, b = results[j]
            if not a.get("started_epoch") or not b.get("started_epoch"):
                continue
            overlap = (a["started_epoch"] < b.get("finished_epoch", 0) and b["started_epoch"] < a.get("finished_epoch", 0))
            if not overlap:
                continue
            ra = set(a.get("resources_held", []))
            rb = set(b.get("resources_held", []))
            global_exclusive = (a.get("exclusive") and not ra) or (b.get("exclusive") and not rb)
            if global_exclusive or (ra & rb):
                errors.append(f"resource overlap: {pa.parent.parent.name}#{a.get('attempt')} vs {pb.parent.parent.name}#{b.get('attempt')}")
    secret_hits = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
        for fn in files:
            p = Path(root) / fn
            try:
                if p.stat().st_size > 5 * 1024 * 1024 or not _is_text(p):
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for pat in SECRET_PATTERNS:
                if pat.search(text):
                    secret_hits.append(str(p.relative_to(run_dir)))
                    break
    loop_state = run_dir / "09-loop" / "state.json"
    if loop_state.exists() or getattr(args, "require_loop", False):
        if not loop_state.exists():
            errors.append("require-loop: missing 09-loop/state.json")
        else:
            st = read_json(loop_state, {})
            if not isinstance(st, dict) or int(st.get("iteration", 0) or 0) < 1:
                errors.append("09-loop/state.json invalid iteration")
            if not st.get("stop") and not st.get("next"):
                errors.append("09-loop/state.json has no next recommendation and is not stopped")
            if st.get("stop") and not st.get("stop_reason"):
                errors.append("09-loop/state.json stopped without stop_reason")
            for name in ["iterations.jsonl", "frontier.md"]:
                if not (run_dir / "09-loop" / name).exists():
                    errors.append(f"missing 09-loop/{name}")
            it = run_dir / "09-loop" / "iterations.jsonl"
            if it.exists() and not it.read_text(encoding="utf-8", errors="replace").strip():
                errors.append("09-loop/iterations.jsonl is empty")
    if secret_hits:
        errors.append("possible secret in run dir: " + ", ".join(sorted(set(secret_hits))[:10]))
    if errors:
        print("CHECK FAILED")
        for e in errors:
            print(" -", e)
        return 1
    print("CHECK OK")
    print(f"  cases: {len(cases)} (P0={sum(1 for c in cases if c.get('priority') == 'P0')})")
    print(f"  findings: {len(findings)}")
    print(f"  attempts: {len(results)}")
    if warnings:
        for w in warnings:
            print("  warn:", w)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sf.py", description="scene-fuzz runner")
    ap.add_argument("--version", action="version", version=f"scene-fuzz {SKILL_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create run layout")
    p.add_argument("--target", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--run-id", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("probe", help="probe host/tools/resources/env")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=_cmd_probe)

    p = sub.add_parser("run", help="run one case")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--repeat", type=int, default=None)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("run-suite", help="resource-aware parallel suite")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--case-list", default="02-scenarios/case-list.json")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--filter", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: run_suite(Path(a.run_dir), a.case_list, a.jobs, a.filter or None, a.dry_run))

    p = sub.add_parser("check", help="evidence gate")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--strict", action="store_true")
    p.add_argument("--require-loop", action="store_true")
    p.set_defaults(func=_cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
