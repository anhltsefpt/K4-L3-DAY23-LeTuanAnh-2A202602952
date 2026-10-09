"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N (mặc định N)."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def last_outage_ts(region: str):
    kills = [e for e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1]["ts"] if kills else None


def wait_health_alert(region: str, since: float, timeout: float):
    """Chờ health checker (process độc lập) phát alert UNHEALTHY cho region sau `since`."""
    end = time.time() + timeout
    while time.time() < end:
        for e in _jsonl(HEALTH):
            if (e.get("event") == "state_change" and e.get("region") == region
                    and e.get("to") == "UNHEALTHY" and e["ts"] >= since):
                return e
        time.sleep(0.5)
    return None


def run(primary: str, target: str, backend: str, auto: bool,
        interval: float = 5.0, threshold: int = 3, alert_wait: float = 30.0) -> dict:
    t_start = time.time()

    # 1 — xác nhận outage: `threshold` lần probe fail LIÊN TIẾP, không tin 1 lần fail
    fails, probes = 0, []
    while fails < threshold:
        ok, reason = hc.probe(primary, 2.0)
        probes.append({"ts": round(time.time(), 3), "ok": ok, "reason": reason})
        fails = 0 if ok else fails + 1
        if ok and time.time() - t_start > interval * threshold * 4:
            step(1, "xac_nhan_outage", region=primary, confirmed=False, probes=probes)
            return {"ok": False, "aborted_at": "1_xac_nhan_outage", "reason": "primary van ready"}
        if fails < threshold:
            time.sleep(interval)
    # Alert của health checker độc lập là nguồn sự thật chính; probe ở trên là xác nhận chéo.
    t_out = last_outage_ts(primary)
    alert = wait_health_alert(primary, t_out or t_start, alert_wait)
    step(1, "xac_nhan_outage", region=primary, confirmed=True, consecutive_fails=fails,
         probes=probes, health_alert_ts=alert and alert["ts"])

    # 2 — mở incident, bấm giờ. t_notified luôn > t_outage.
    t_notified = time.time()
    step(2, "thong_bao_incident", severity="SEV1", t_outage=t_out, t_notified=t_notified,
         notify_delay_s=None if t_out is None else round(t_notified - t_out, 2),
         msg=f"region-{primary} down, failover sang region-{target}")

    if not confirm(auto, f"Failover tu region-{primary} sang region-{target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator khong confirm")
        return {"ok": False, "aborted_at": "3_scale_gpu_pool", "reason": "operator_declined"}

    # 3 — gọi failover ĐÚNG 1 LẦN (tự làm verify/restore/scale/wait/cutover)
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", operator_confirmed=True, auto=auto, failover_ok=result.get("ok"),
         warmup_waited_s=result.get("warmup_waited_s"), failed_step=result.get("failed_step"))
    if not result.get("ok"):
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - t_start, 2),
             note="failover abort -> escalate, KHONG cutover")
        return {"ok": False, "aborted_at": "3_scale_gpu_pool", "failover": result}

    # 4 — chỉ đọc lại kết quả từ dict của bước 3
    after = result.get("after") or {}
    step(4, "verify_state_replica", region=target, vectors=after.get("count"),
         weights=after.get("weights"), pool_state=after.get("pool_state"),
         rpo_seconds=result.get("rpo_seconds"), docs_lost=result.get("docs_lost"),
         embed_model_version=result.get("embed_model_version"))

    # 5 — chỉ đọc lại: cutover ok?
    step(5, "dns_cutover", ok=result.get("ok"), active_region=target)

    # 6 — golden signals: 10 request thật vào region phụ
    lat, errs = [], 0
    for i in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", params={"q": f"golden {i}"}, timeout=2.0)
            errs += r.status_code != 200
        except Exception:
            errs += 1
        lat.append((time.time() - t0) * 1000)
    lat.sort()
    p95 = round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1)
    step(6, "verify_golden_signals", region=target, requests=10, p95_ms=p95,
         error_rate=errs / 10, pass_=errs == 0)

    # 7 — post-incident
    elapsed = round(time.time() - t_start, 2)
    step(7, "post_incident", ok=True, elapsed_s=elapsed,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": True, "primary": primary, "target": target, "elapsed_s": elapsed,
            "rpo_seconds": result.get("rpo_seconds"), "docs_lost": result.get("docs_lost"),
            "golden": {"p95_ms": p95, "error_rate": errs / 10}}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
