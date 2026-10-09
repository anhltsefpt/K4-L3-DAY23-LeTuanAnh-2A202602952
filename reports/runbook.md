# Runbook 1 trang — Region chính down

**Sinh viên:** Lê Tuấn Anh — **MSSV:** 2A202602952

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong. Chạy từ thư mục gốc của lab.

**Cách nhanh (bán tự động, khuyến nghị):** `python3 dr/runbook.py --primary a --target b --backend fs`
— tự làm 7 bước dưới, hỏi `y/N` trước khi failover. Log: `reports/runbook-run.jsonl`,
`reports/failover-events.jsonl`. Các lệnh từng bước bên dưới dùng khi script lỗi.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách 5s) và `tail -n 3 reports/health-events.jsonl` | `a.ready=false` 3 lần liên tiếp **và** có dòng `"region": "a", "to": "UNHEALTHY"` | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | Mở kênh `#inc-<ngày>-region-a`, ghi SEV1 + giờ UTC: `date -u +%FT%TZ` | Có ts trong `reports/runbook-run.jsonl` (step 2) / kênh incident | On-call SRE (Incident Commander) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` rồi `python3 state/snapshot.py get --region b --backend fs` | `curl -s localhost:8002/v1/state` cho `count > 0`, `weights: true`; ghi lại `rpo_seconds` | On-call SRE |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi `curl -s -o /dev/null -w '%{http_code}\n' localhost:8002/readyz` (lặp tới 200, ~6s warm-up) | `/readyz` của b trả 200. Quá 60s chưa 200 → **DỪNG, không cutover**, escalate | On-call SRE |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `active_region=b` (chờ tối đa TTL 5s) | On-call SRE, sau khi IC đồng ý |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8080/v1/infer; done` | 10/10 trả 200, p95 < 500ms, error rate < 1% | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` = `PASS`, `valid: true`; mở postmortem trong 48h | Incident Commander |

**Rollback (failover ngược về A):**
- **Điều kiện rollback ngay về A** (trong lúc failover): bước 6 thất bại (error rate ≥ 1% hoặc
  p95 ≥ 500ms ở B) **và** A đã phục hồi `ready` ổn định.
- **Failback có kế hoạch:** chỉ khi A `HEALTHY` liên tục ≥ 30 phút trong `reports/health-events.jsonl`,
  đã reverse-replicate dữ liệu B → A (ingest phát sinh trong lúc chạy ở B), và trong giờ hành chính.
  Lệnh: `python3 dr/failover.py --target a --backend fs` (sau khi snapshot B lên object store).
- **Ai quyết định:** Incident Commander (on-call lead). Không bao giờ để automation tự failback —
  full-auto không có circuit breaker sẽ làm 2 region flap qua lại (§4 Anti-Patterns).
  Tối đa 1 lần failover mỗi chiều trong 1 giờ.
