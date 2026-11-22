#!/usr/bin/env python3
"""Tiny CLI for demos and grading — stdlib only.

  python3 scripts/nt.py submit <youtube_url> [--token T] [--parent ID]   submit + watch
  python3 scripts/nt.py watch <job_id>                                  live status
  python3 scripts/nt.py burst <url> <url> ...  [--token T]              burst test + latency table
  python3 scripts/nt.py chaos <youtube_url> [--stage EXTRACTING_FRAMES] kill the worker holding the job mid-stage
  python3 scripts/nt.py faults '{"llm_429": 2}'                          arm fault injection
  python3 scripts/nt.py health

Env: GATEWAY (default http://localhost:8000), NOTION_TOKEN, ADMIN_TOKEN (default dev-admin).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

GW = os.getenv("GATEWAY", "http://localhost:8000").rstrip("/")
ADMIN = os.getenv("ADMIN_TOKEN", "dev-admin")
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(GW + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Admin-Token": ADMIN, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def arg(name: str, default: str | None = None) -> str | None:
    if name in sys.argv:
        i = sys.argv.index(name)
        val = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return val
    return default


def submit(url: str, token: str | None, parent: str | None) -> str:
    body = {"youtube_url": url}
    if token:
        body["notion_token"] = token
    if parent:
        body["notion_parent_id"] = parent
    code, res = call("POST", "/jobs", body)
    if code != 202:
        sys.exit(f"submit failed ({code}): {res}")
    return res["job_id"]


def line(j: dict) -> str:
    extra = ""
    if j.get("retry"):
        r = j["retry"]
        extra = f"  ⏳ {r['code']} retry {r['attempt']}/{r['max_attempts']} in {max(0, round(r['retry_at'] - time.time()))}s"
    elif j.get("waiting"):
        extra = f"  ⏳ pacing {j['waiting']['service']}"
    w = (j.get("worker") or "").split(":")[0]
    return f"{j['status']:<24} {j['progress']:>3}%  worker={w:<12}{extra}"


def watch(job_id: str, quiet: bool = False) -> dict:
    last, seen_events = None, 0
    t0 = time.time()
    while True:
        code, j = call("GET", f"/jobs/{job_id}")
        if code != 200:
            sys.exit(f"watch failed: {j}")
        events = j.get("events") or []
        if not quiet:
            for e in events[seen_events:]:
                print(f"   • {e['kind']}: {e['message']}")
            seen_events = len(events)
            cur = line(j)
            if cur != last:
                print(f"[{time.time() - t0:6.1f}s] {cur}")
                last = cur
        if j["status"] in TERMINAL:
            code, res = call("GET", f"/jobs/{job_id}/results")
            if not quiet:
                print(json.dumps(res, indent=2))
            return j
        time.sleep(1)


def cmd_burst(urls: list[str], token: str | None) -> None:
    t0 = time.time()
    ids = [(u, submit(u, token, None)) for u in urls]
    print(f"submitted {len(ids)} jobs in {time.time() - t0:.2f}s")
    pending = {jid for _, jid in ids}
    done: dict[str, dict] = {}
    while pending:
        for jid in list(pending):
            _, j = call("GET", f"/jobs/{jid}")
            if j["status"] in TERMINAL:
                j["_latency"] = j["updated_at"] - j["created_at"]
                done[jid] = j
                pending.discard(jid)
        st = {}
        for jid in pending:
            s = call("GET", f"/jobs/{jid}")[1]["status"]
            st[s] = st.get(s, 0) + 1
        print(f"[{time.time() - t0:6.1f}s] done {len(done)}/{len(ids)}  in-flight {st}", flush=True)
        if pending:
            time.sleep(3)
    print(f"\n{'status':<24} {'latency':>8} {'frames':>7}  title")
    lats = []
    for _, jid in ids:
        j = done[jid]
        lats.append(j["_latency"])
        r = j.get("result") or {}
        fr = f"{r.get('frames_published', '-')}/{r.get('frames_requested', '-')}"
        print(f"{j['status']:<24} {j['_latency']:>7.1f}s {fr:>7}  {(j.get('video_title') or j['video_id'])[:50]}")
    lats.sort()
    _, m = call("GET", "/admin/overview")
    met = m.get("metrics", {})
    print(f"\ndrain time {time.time() - t0:.1f}s · p50 {lats[len(lats) // 2]:.1f}s · max {lats[-1]:.1f}s")
    print(f"LLM calls {met.get('llm_calls', 0)} · provider 429s {met.get('llm_provider_429', 0)} "
          f"· self-paced waits {met.get('ratelimit_wait_sec:llm', 0)}s")


def cmd_chaos(url: str, token: str | None, stage: str) -> None:
    jid = submit(url, token, None)
    print(f"job {jid} — waiting for {stage} …")
    while True:
        _, j = call("GET", f"/jobs/{jid}")
        if j["status"] == stage and j.get("worker"):
            break
        if j["status"] in TERMINAL:
            sys.exit(f"job finished before reaching {stage}: {j['status']}")
        time.sleep(0.3)
    victim = j["worker"].split(":")[0]  # container hostname == container id
    time.sleep(1.0)
    print(f"💥 docker kill {victim}  (worker holding the job at {j['progress']}%)")
    t_kill = time.time()
    subprocess.run(["docker", "kill", victim], check=False, capture_output=True)
    resumed = None
    while True:
        _, j = call("GET", f"/jobs/{jid}")
        w = (j.get("worker") or "").split(":")[0]
        if resumed is None and w and w != victim:
            resumed = time.time() - t_kill
            print(f"🔄 resumed on {w} after {resumed:.1f}s")
        if j["status"] in TERMINAL:
            break
        time.sleep(0.5)
    print(f"final: {j['status']} total recovery+finish {time.time() - t_kill:.1f}s")
    print(json.dumps(call("GET", f"/jobs/{jid}/results")[1], indent=2))
    print("events:")
    for e in j.get("events", []):
        print(f"   • {e['kind']}: {e['message']}")
    print("\nRestart the killed worker with: docker compose up -d --scale worker=2")


def main() -> None:
    token = arg("--token", os.getenv("NOTION_TOKEN"))
    parent = arg("--parent")
    stage = arg("--stage", "EXTRACTING_FRAMES")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "submit":
        watch(submit(rest[0], token, parent))
    elif cmd == "watch":
        watch(rest[0])
    elif cmd == "burst":
        cmd_burst(rest, token)
    elif cmd == "chaos":
        cmd_chaos(rest[0], token, stage)
    elif cmd == "faults":
        print(call("POST", "/admin/faults", json.loads(rest[0]))[1])
    elif cmd == "health":
        print(json.dumps(call("GET", "/healthz")[1], indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
