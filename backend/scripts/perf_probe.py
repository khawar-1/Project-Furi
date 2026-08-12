"""
Furi OS — whole-machine performance probe (idle footprint + chat turn latency).

WHY THIS EXISTS. Two complaints — "make Furi fast" and "it makes the laptop
stuck and crashes the opened things" — had no number behind either of them. The
app already measures a chat turn (app/core/timing.py) but nothing had ever read
the line back; and nothing at all measured what Furi costs the MACHINE while
sitting there, which is the half that produces
`STATUS_COMMITMENT_LIMIT` (0xC000012D) in ~/.jarvis/logs/renderer-crashes.log.

Two independent modes, because they answer different questions:

  --idle    Samples the machine for N seconds: per-process-group CPU and RAM,
            system commit charge, and VRAM. No chat, no backend needed. This is
            the one that answers "what does Furi cost when I am not using it",
            and it is how the wake-word / screen-OCR decision gets made:
            run it with the feature on, run it again with it off, diff the two.

  --turns   Drives real chat turns against a RUNNING backend and reports
            time-to-first-token measured client-side, alongside the backend's
            own per-stage timing line for the same turn.

MEASUREMENT NOTES, because a wrong number is worse than none:

  * CPU% is derived from the DELTA in per-process CPU-seconds across a sample
    interval, divided by wall time and logical core count. A single
    Get-Process snapshot reports CUMULATIVE CPU since process start, which is
    not a rate — reading it as one would make a long-lived process look pegged.
  * Commit charge, not "RAM used", is the number that predicts the crash. The
    OS refuses a commit when the charge hits the limit, regardless of how much
    physical RAM looks free.
  * VRAM comes from nvidia-smi. WMI's AdapterRAM truncates (it reports 4 GB for
    a 6 GB card) and must not be used.
  * Always pass --label and compare runs from the SAME session. A number from
    another day is not a control — the 2026-08-01 browse_speed round spent an
    afternoon on a "regression" that was the machine being busier.

No new dependency: psutil is not installed and a bench script is not a reason to
add one, so this shells out to PowerShell and nvidia-smi.

Run from backend/:

    venv\\Scripts\\python -u scripts\\perf_probe.py --idle 60 --label wake-on
    venv\\Scripts\\python -u scripts\\perf_probe.py --idle 60 --label wake-off
    venv\\Scripts\\python -u scripts\\perf_probe.py --turns
    venv\\Scripts\\python -u scripts\\perf_probe.py --compare wake-on wake-off

NEVER collected by pytest (lives outside tests/, measures the real machine).
Results are written to scripts/bench-results/ so a change is a DELTA.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# A legacy cp1252 console mangles em-dashes in exactly the block you most need
# to read (the 2026-08-03 bench lesson). Force UTF-8 and keep our own literals
# ASCII-only.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BACKEND_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"

# Process-name -> group. Furi is several process trees and the interesting
# question is which TREE is spending, not which pid.
#
# chrome/msedge are deliberately NOT here: the user's own browser has the same
# image name as the browser agent's, and the first cut of this script charged
# 3.9 GB of a personal Chrome to Furi. They are classified by command line
# instead (see _classify_browser) and split into two groups, only one of which
# counts toward the Furi total.
GROUPS = {
    "python": "backend (python)",
    "node": "frontend/dev (node)",
    "electron": "app shell (electron)",
    "powershell": "sensing helper (powershell)",
    "pwsh": "sensing helper (powershell)",
}

BROWSER_NAMES = ("chrome", "msedge")
JARVIS_BROWSER_GROUP = "browser agent (jarvis)"
OTHER_BROWSER_GROUP = "other browser (NOT jarvis)"

# The marker every agent-driven Chrome carries: browser/session.py launches the
# persistent context, the sign-in window and the clean window all against
# ~/.jarvis/browser, and reclaim_orphaned_profile already identifies them the
# same way.
_JARVIS_PROFILE_MARKER = r"\.jarvis\browser"

# pid -> is-a-jarvis-browser. A pid is classified once; Win32_Process carries
# CommandLine but is a WMI query costing 1-3s, far too slow to run per sample.
_browser_class: dict[int, bool] = {}


# --------------------------------------------------------------- machine probe

def _ps(script: str) -> str:
    """Run a PowerShell snippet and return stdout ('' on any failure)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout or ""
    except Exception:
        return ""


def logical_cores() -> int:
    return os.cpu_count() or 1


def _classify_browser(pids: list[int]) -> None:
    """Decide which of `pids` are Furi's browser, by command line.

    Only pids not already classified are queried, so a steady set of browser
    windows costs one WMI query for the whole run. An unclassifiable pid is
    recorded as NOT-Furi: over-charging the user's own browser to Furi is
    the failure this function exists to prevent, so ambiguity resolves away
    from us.
    """
    unknown = [p for p in pids if p not in _browser_class]
    if not unknown:
        return
    clause = " or ".join(f"ProcessId={p}" for p in unknown)
    raw = _ps(
        f'Get-CimInstance Win32_Process -Filter "{clause}" | '
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    rows = []
    try:
        rows = json.loads(raw) if raw.strip() else []
    except Exception:
        rows = []
    if isinstance(rows, dict):
        rows = [rows]
    seen: dict[int, bool] = {}
    for r in rows:
        try:
            cmd = (r.get("CommandLine") or "").lower()
            seen[int(r["ProcessId"])] = _JARVIS_PROFILE_MARKER in cmd
        except Exception:
            continue
    for p in unknown:
        _browser_class[p] = seen.get(p, False)


def sample_processes() -> dict[int, tuple[str, float, float]]:
    """pid -> (group, cpu_seconds_cumulative, working_set_mb).

    CPU here is CUMULATIVE since process start; the caller turns two samples
    into a rate. Never report this value directly as a percentage.
    """
    names = ",".join(sorted(set(GROUPS) | set(BROWSER_NAMES)))
    raw = _ps(
        f"Get-Process {names} -ErrorAction SilentlyContinue | "
        "Select-Object Id,ProcessName,CPU,WorkingSet64 | ConvertTo-Json -Compress"
    )
    if not raw.strip():
        return {}
    try:
        rows = json.loads(raw)
    except Exception:
        return {}
    if isinstance(rows, dict):  # PowerShell emits a bare object for one row
        rows = [rows]

    parsed: list[tuple[int, str, float, float]] = []
    browser_pids: list[int] = []
    for r in rows:
        try:
            pid = int(r["Id"])
            name = str(r.get("ProcessName", "")).lower()
            cpu = float(r.get("CPU") or 0.0)
            rss = float(r.get("WorkingSet64") or 0) / (1024 * 1024)
        except Exception:
            continue
        parsed.append((pid, name, cpu, rss))
        if name in BROWSER_NAMES:
            browser_pids.append(pid)

    _classify_browser(browser_pids)

    out: dict[int, tuple[str, float, float]] = {}
    for pid, name, cpu, rss in parsed:
        if name in BROWSER_NAMES:
            group = (JARVIS_BROWSER_GROUP if _browser_class.get(pid)
                     else OTHER_BROWSER_GROUP)
        else:
            group = GROUPS.get(name, "")
        if not group:
            continue
        out[pid] = (group, cpu, rss)
    return out


def sample_system() -> dict:
    """Commit charge and physical memory, in GB."""
    raw = _ps(
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "[pscustomobject]@{"
        "  commit_limit_gb  = [math]::Round($os.TotalVirtualMemorySize/1MB,2);"
        "  commit_used_gb   = [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory)/1MB,2);"
        "  phys_total_gb    = [math]::Round($os.TotalVisibleMemorySize/1MB,2);"
        "  phys_free_gb     = [math]::Round($os.FreePhysicalMemory/1MB,2)"
        "} | ConvertTo-Json -Compress"
    )
    try:
        return json.loads(raw)
    except Exception:
        return {}


def sample_vram() -> dict:
    """Total/used/free VRAM in MiB from nvidia-smi (authoritative; WMI truncates)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        total, used, free = [int(x.strip()) for x in out.stdout.split(",")]
        return {"vram_total_mib": total, "vram_used_mib": used, "vram_free_mib": free}
    except Exception:
        return {}


# ------------------------------------------------------------------ idle mode

def run_idle(seconds: int, interval: float, label: str) -> dict:
    cores = logical_cores()
    print(f"Sampling the machine for {seconds}s (every {interval}s), {cores} logical cores.")
    print("Leave Furi alone for the duration - this measures the IDLE cost.\n")

    prev = sample_processes()
    prev_t = time.perf_counter()
    per_group_cpu: dict[str, list[float]] = {}
    per_group_ram: dict[str, list[float]] = {}
    commit: list[float] = []
    vram: list[float] = []
    proc_counts: dict[str, list[int]] = {}

    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        time.sleep(interval)
        cur = sample_processes()
        now = time.perf_counter()
        elapsed = now - prev_t

        cpu_by_group: dict[str, float] = {}
        ram_by_group: dict[str, float] = {}
        n_by_group: dict[str, int] = {}
        for pid, (group, cpu_s, rss) in cur.items():
            # A pid absent from the previous sample is new; its cumulative CPU
            # is not a delta we can attribute to this interval, so count only
            # its memory and let the next interval measure its rate.
            if pid in prev:
                delta = cpu_s - prev[pid][1]
                if delta >= 0:
                    cpu_by_group[group] = cpu_by_group.get(group, 0.0) + delta
            ram_by_group[group] = ram_by_group.get(group, 0.0) + rss
            n_by_group[group] = n_by_group.get(group, 0) + 1

        for g, secs in cpu_by_group.items():
            per_group_cpu.setdefault(g, []).append(100.0 * secs / elapsed / cores)
        for g, mb in ram_by_group.items():
            per_group_ram.setdefault(g, []).append(mb)
        for g, n in n_by_group.items():
            proc_counts.setdefault(g, []).append(n)

        sysinfo = sample_system()
        if sysinfo:
            commit.append(sysinfo.get("commit_used_gb", 0.0))
        v = sample_vram()
        if v:
            vram.append(v.get("vram_used_mib", 0))

        prev, prev_t = cur, now
        print(".", end="", flush=True)
    print("\n")

    def stats(xs: list[float]) -> dict:
        if not xs:
            return {"mean": 0.0, "p95": 0.0, "max": 0.0}
        s = sorted(xs)
        return {
            "mean": round(sum(s) / len(s), 2),
            "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 2),
            "max": round(s[-1], 2),
        }

    result = {
        "label": label,
        "kind": "idle",
        "when": datetime.now().isoformat(timespec="seconds"),
        "seconds": seconds,
        "logical_cores": cores,
        "groups": {
            g: {
                "cpu_pct": stats(per_group_cpu.get(g, [])),
                "ram_mb": stats(per_group_ram.get(g, [])),
                "processes": max(proc_counts.get(g, [0])),
            }
            for g in sorted(set(per_group_cpu) | set(per_group_ram))
        },
        "commit_used_gb": stats(commit),
        "vram_used_mib": stats(vram),
        "system": sample_system(),
        "vram": sample_vram(),
    }
    _print_idle(result)
    return result


def _print_idle(r: dict) -> None:
    print(f"=== IDLE FOOTPRINT [{r['label']}] {r['when']} ===")
    print(f"{'group':<28} {'procs':>5} {'CPU% mean':>10} {'CPU% p95':>9} {'RAM MB':>9}")
    print("-" * 65)
    total_cpu = total_ram = 0.0
    for g, d in sorted(r["groups"].items(), key=lambda kv: -kv[1]["cpu_pct"]["mean"]):
        print(f"{g:<28} {d['processes']:>5} {d['cpu_pct']['mean']:>10.2f} "
              f"{d['cpu_pct']['p95']:>9.2f} {d['ram_mb']['mean']:>9.0f}")
        # The user's own browser shares an image name with the agent's and is
        # shown for context (it competes for the same commit charge), but it is
        # not Furi's cost and must never inflate the headline.
        if g != OTHER_BROWSER_GROUP:
            total_cpu += d["cpu_pct"]["mean"]
            total_ram += d["ram_mb"]["mean"]
    print("-" * 65)
    print(f"{'TOTAL (Furi only)':<28} {'':>5} {total_cpu:>10.2f} {'':>9} {total_ram:>9.0f}")
    sysinfo = r.get("system") or {}
    v = r.get("vram") or {}
    print()
    print(f"commit charge : {r['commit_used_gb']['mean']:.2f} GB mean, "
          f"{r['commit_used_gb']['max']:.2f} GB peak "
          f"of {sysinfo.get('commit_limit_gb', '?')} GB limit")
    print(f"physical free : {sysinfo.get('phys_free_gb', '?')} GB "
          f"of {sysinfo.get('phys_total_gb', '?')} GB")
    if v:
        print(f"VRAM          : {r['vram_used_mib']['mean']:.0f} MiB mean, "
              f"{r['vram_used_mib']['max']:.0f} MiB peak of {v.get('vram_total_mib')} MiB")
    print()


# ----------------------------------------------------------------- turns mode

TURNS = [
    ("plain-chat", "how are you today"),
    ("plain-chat-2", "what did you think of that"),
    ("routed-read", "what files are on my desktop"),
]


def _auth_token() -> str:
    tok = os.environ.get("JARVIS_TOKEN")
    if tok:
        return tok
    p = Path.home() / ".jarvis" / "auth_token"
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def run_turns(base_url: str, label: str) -> dict:
    import httpx
    import uuid

    token = _auth_token()
    if not token:
        print("No auth token found (~/.jarvis/auth_token or $JARVIS_TOKEN).")
        return {}

    headers = {"X-Jarvis-Token": token, "Content-Type": "application/json"}
    session_id = f"perf-{uuid.uuid4().hex[:8]}"
    rows = []

    print(f"Driving {len(TURNS)} turns against {base_url} (session {session_id}).")
    print("TTFT is measured CLIENT-side; the backend's own stage line is in")
    print("~/.jarvis/logs/backend.log for the same session.\n")

    history: list[dict] = []
    for name, text in TURNS:
        history.append({"role": "user", "content": text})
        body = {"messages": history, "session_id": session_id}
        t0 = time.perf_counter()
        ttft = None
        chars = 0
        try:
            with httpx.stream("POST", f"{base_url}/chat/stream", json=body,
                              headers=headers, timeout=180.0) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
                    try:
                        chunk = json.loads(line[6:])
                    except Exception:
                        continue
                    chars += len(chunk.get("delta") or "")
                    if chunk.get("done"):
                        break
        except Exception as e:
            print(f"  {name:<14} FAILED: {type(e).__name__}: {e}")
            rows.append({"turn": name, "error": f"{type(e).__name__}: {e}"})
            continue
        total = (time.perf_counter() - t0) * 1000
        history.append({"role": "assistant", "content": "(elided)"})
        print(f"  {name:<14} ttft={ttft or 0:>8.0f}ms  total={total:>8.0f}ms  {chars} chars")
        rows.append({"turn": name, "ttft_ms": round(ttft or 0), "total_ms": round(total),
                     "chars": chars})

    result = {
        "label": label, "kind": "turns", "session_id": session_id,
        "when": datetime.now().isoformat(timespec="seconds"), "turns": rows,
    }
    done = [r for r in rows if "ttft_ms" in r]
    if done:
        print(f"\n  median ttft: {sorted(r['ttft_ms'] for r in done)[len(done)//2]}ms")
    print(f"\n  Backend stage breakdown:")
    print(f"    grep '{session_id[:8]}' ~/.jarvis/logs/backend.log")
    return result


# -------------------------------------------------------------------- compare

def run_compare(a: str, b: str) -> None:
    ra, rb = _load(a), _load(b)
    if not ra or not rb:
        print("Both labels must exist in scripts/bench-results/.")
        return
    if ra.get("kind") != "idle" or rb.get("kind") != "idle":
        print("Compare currently supports --idle runs only.")
        return
    print(f"=== {a}  ->  {b} ===")
    print(f"{'group':<28} {'CPU% A':>8} {'CPU% B':>8} {'delta':>9} {'RAM A':>8} {'RAM B':>8}")
    print("-" * 74)
    for g in sorted(set(ra["groups"]) | set(rb["groups"])):
        ga = ra["groups"].get(g, {}).get("cpu_pct", {}).get("mean", 0.0)
        gb = rb["groups"].get(g, {}).get("cpu_pct", {}).get("mean", 0.0)
        ma = ra["groups"].get(g, {}).get("ram_mb", {}).get("mean", 0.0)
        mb = rb["groups"].get(g, {}).get("ram_mb", {}).get("mean", 0.0)
        print(f"{g:<28} {ga:>8.2f} {gb:>8.2f} {gb-ga:>+9.2f} {ma:>8.0f} {mb:>8.0f}")
    print("-" * 74)
    ca = ra["commit_used_gb"]["mean"]
    cb = rb["commit_used_gb"]["mean"]
    va = ra["vram_used_mib"]["mean"]
    vb = rb["vram_used_mib"]["mean"]
    print(f"{'commit charge (GB)':<28} {ca:>8.2f} {cb:>8.2f} {cb-ca:>+9.2f}")
    print(f"{'VRAM used (MiB)':<28} {va:>8.0f} {vb:>8.0f} {vb-va:>+9.0f}")


def _save(result: dict) -> None:
    if not result:
        return
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"perf-{result['kind']}-{result['label']}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {path}")


def _load(label: str) -> dict:
    for kind in ("idle", "turns"):
        p = RESULTS_DIR / f"perf-{kind}-{label}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Furi whole-machine performance probe")
    ap.add_argument("--idle", type=int, metavar="SECONDS",
                    help="sample the idle footprint for N seconds")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="sampling interval in seconds (default 2)")
    ap.add_argument("--turns", action="store_true",
                    help="drive real chat turns against a running backend")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--label", default="baseline",
                    help="tag this run so it can be diffed later")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"),
                    help="diff two saved --idle runs")
    args = ap.parse_args()

    if args.compare:
        run_compare(*args.compare)
        return 0
    if args.idle:
        _save(run_idle(args.idle, args.interval, args.label))
        return 0
    if args.turns:
        _save(run_turns(args.base_url.rstrip("/"), args.label))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
