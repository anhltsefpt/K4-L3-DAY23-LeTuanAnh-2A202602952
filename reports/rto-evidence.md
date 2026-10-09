# RTO/RPO Evidence — Lab 23

**Sinh viên:** Lê Tuấn Anh — **MSSV:** 2A202602952

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Mọi giờ là UTC. Mốc 0 của Drill 2 = `ts 1791575942.49` (chaos kill).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T19:58:03Z` | chaos kill, `mode=netblock` (SIGSTOP) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.1s | dòng `ok:false` đầu tiên sau t_outage (503 từ edge) | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | 16/32 request fail, dòng cuối vẫn `ok:false` | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl` | `reports/drill-1-nodr.jsonl:17` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill`, region a, netblock | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu (`ReadTimeout`) | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +19.4s | `to:UNHEALTHY, region:a`, `consecutive_fails=3` | `reports/health-events.jsonl:3` |
| Snapshot restore xong | +19.9s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +26.5s | `step:4_wait_ready`, `waited_s=6.56` | `reports/failover-events.jsonl:4` |
| DNS cutover | +26.5s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.5s** | dòng `ok:true` đầu sau lỗi, `served_by=b` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.5s | 300s (5 phút) | PASS (dư 271.5s) |
| RPO — Vector DB | 8.02s / 4 doc | 300s (5 phút) | PASS |

RPO lấy từ `rpo_seconds` / `docs_lost` ở `reports/failover-events.jsonl:2`; snapshot được dùng
là chu kỳ replication tại `reports/replication.jsonl:2` (`every_s=30` → RPO lý thuyết tối đa ~30s).

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect | 19.4s | floor = `interval_s × threshold` = 5 × 3 = 15.0s (`reports/health-events.jsonl:3`), + 2s probe timeout + lệch pha poll | Giảm interval (vd 2s × 3 = 6s) — đổi lại tăng rủi ro flapping và tải probe |
| Snapshot restore | 0.5s | 19.4s → 19.9s: runbook xác nhận/thông báo + `2_restore` → `3_scale` | Đã nhỏ; với index thật (GB) nên pre-restore liên tục vào region B (warm standby) |
| GPU pool warm-up | 6.6s | `waited_s=6.56` ở `reports/failover-events.jsonl:4` | Giữ region B ở `full` (hot standby) — đổi lại tốn chi phí GPU chạy không |
| DNS/LB TTL cache | 2.0s | t_recovered − t_cutover = 28.5s − 26.5s | Hạ `EDGE_TTL_SECONDS`, hoặc dùng LB anycast thay DNS |
| **Tổng** | **28.5s** | 19.4 + 0.5 + 6.6 + 2.0 | |
