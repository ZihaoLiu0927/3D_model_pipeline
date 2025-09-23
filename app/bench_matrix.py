# bench_matrix.py
import concurrent.futures as cf, time, requests, os, json, statistics as st
from pathlib import Path

BASE = "http://127.0.0.1:8000"
MODELS = [r"G:\downloads\Lidded-Ewer.obj", r"G:\downloads\Dragon.3mf"]

def run_one(path):
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        r = requests.post(f"{BASE}/process", files={"file": (os.path.basename(path), f)})
        r.raise_for_status()
        task = r.json()["task_id"]
    while True:
        time.sleep(0.5)
        j = requests.get(f"{BASE}/result/{task}").json()
        if j["state"] == "SUCCESS":
            t1 = time.perf_counter()
            metrics = j.get("validate_report", {}).get("metrics", {})
            return {
                "file": os.path.basename(path),
                "wall": t1 - t0,
                **metrics
            }
        elif j["state"] not in ("PENDING","STARTED"):
            return {"file": os.path.basename(path), "error": j}

def run_batch(concurrency:int):
    results = []
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(run_one, p) for p in (MODELS * ((concurrency+len(MODELS)-1)//len(MODELS)))[:concurrency]]
        for f in cf.as_completed(futs):
            results.append(f.result())
    elapsed = time.perf_counter() - t0
    return results, elapsed

def summarize(rs):
    ok = [r for r in rs if "error" not in r]
    walls = [r["wall"] for r in ok]
    rss   = [r.get("peak_rss_mb", 0) for r in ok]
    cpu_s = [r.get("cpu_user_seconds",0)+r.get("cpu_sys_seconds",0) for r in ok]
    wr_mb = [r.get("io_write_mb",0) for r in ok]
    return {
        "count": len(ok),
        "p50_wall": round(st.median(walls),3) if walls else None,
        "p95_wall": round(st.quantiles(walls, n=20)[18],3) if len(walls)>=20 else None,
        "avg_cpu_seconds": round(sum(cpu_s)/len(cpu_s),3) if cpu_s else None,
        "p95_rss_mb": round(st.quantiles(rss, n=20)[18],1) if len(rss)>=20 else (max(rss) if rss else None),
        "avg_write_mb": round(sum(wr_mb)/len(wr_mb),1) if wr_mb else None,
    }

if __name__ == "__main__":
    for c in [1,2,4,8]:
        rs, el = run_batch(c)
        s = summarize(rs)
        print(json.dumps({"concurrency": c, "elapsed_s": round(el,3), **s}, ensure_ascii=False))
