# Postmortem — DR Drill Lab 23

**Sinh viên:** Lê Tuấn Anh — **MSSV:** 2A202602952

Theo template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (UTC, mọi dòng có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T19:59:02.49Z | outage bắt đầu (region a netblock / SIGSTOP) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T19:59:02.62Z | user đầu tiên bị ảnh hưởng (`ReadTimeout`, +0.1s) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T19:59:21.92Z | health check alert: region a `UNHEALTHY` (+19.4s) | `reports/health-events.jsonl:3` |
| 2026-10-09T19:59:22.41Z | operator confirm (`--auto`) → failover bắt đầu (+19.9s) | `reports/failover-events.jsonl:1` |
| 2026-10-09T19:59:28.99Z | DNS cutover sang region b (+26.5s) | `reports/failover-events.jsonl:5` |
| 2026-10-09T19:59:31.01Z | resolved: request đầu tiên OK từ region b (+28.5s) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: 28.5s · gap: −271.5s (đạt, còn dư)
- RPO mục tiêu: 300s · đo được: 8.02s (4 doc bị mất) · gap: −292s (đạt). RPO lý thuyết xấu nhất ≈ chu kỳ replicate 30s.
- **Bước tốn nhiều giây nhất:** health-check detection (19.4s ≈ 68% RTO). Floor cứng
  `5s × 3 = 15s`, cộng thêm 2s timeout mỗi probe (netblock làm request treo chứ không fail
  ngay) và độ lệch pha giữa lúc kill với chu kỳ poll.

## 3. Root cause (5 whys)

*Nếu đây là outage thật, bước nào trong runbook sẽ thất bại?*

1. Vì sao user lỗi 28.5s? → Edge vẫn trỏ region a cho tới khi failover cutover.
2. Vì sao cutover muộn? → Phải chờ health check xác nhận 3 lần fail liên tiếp, rồi chờ GPU pool B warm-up.
3. Vì sao B phải warm-up và restore? → Kiến trúc active-passive với B ở `warm`, rỗng dữ liệu cho tới khi restore snapshot.
4. Vì sao mất 4 doc? → Replication là snapshot định kỳ 30s, không phải streaming; doc ingest sau snapshot cuối bị mất.
5. Điểm yếu thật nếu outage thật: snapshot được `put` từ chính region a — nếu a mất đĩa
   trước chu kỳ replicate kế tiếp, RPO = tối đa 30s; và nếu object store đặt cùng region a
   thì không có gì để restore. Ngoài ra `replicate.py` không cảnh báo khi một chu kỳ `put` thất bại.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giảm health-check interval 5s → 2s (giữ threshold 3), timeout 2s → 1s | SRE on-call lead | 2026-10-24 | RTO −9s (floor 15s → 6s) |
| 2 | Replication 30s → 10s + alert khi `snapshot lag` > 60s | Data platform | 2026-10-31 | RPO xấu nhất −20s |
| 3 | Giữ B ở hot standby (`full`) giờ cao điểm | Infra / FinOps | 2026-11-15 | RTO −6.6s (bỏ warm-up), tăng chi phí GPU |
| 4 | Hạ edge TTL 5s → 1s | Network | 2026-10-24 | RTO −~2s |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` = 5 × 3 = **15s**, chiếm 15 / 28.5 ≈ **53%** RTO (phần detection
   thực tế 19.4s ≈ 68%).
2. Hạ interval xuống 1s: floor còn 3s, RTO giảm khoảng 12–15s (~14s). Cái giá: 1 sự cố
   mạng ngắn (GC pause, packet loss 3s) đã đủ trigger failover → flapping qua lại giữa 2
   region, mỗi lần flap lại tốn warm-up + restore và có thể mất thêm dữ liệu; tải probe tăng 5 lần.
3. Nếu region a mất dữ liệu vĩnh viễn, `docs_lost=4` nghĩa là 4 ticket khách hàng đã ingest
   (khách tưởng đã lưu) biến mất vĩnh viễn khỏi hệ thống RAG — câu trả lời của AI sẽ không
   biết về chúng; phải thông báo cho khách và yêu cầu gửi lại / re-ingest từ nguồn gốc.
   Outage 6h không làm `docs_lost` tăng (đã chuyển sang B), nhưng mọi ingest trong 6h đó
   phải được ghi vào B — khi failback về A phải reverse-replicate B → A trước.

## 6. Câu hỏi trong `dr/health_checker.py` + Reflection (GUIDE.md)

**Phát hiện sớm nhất với interval=5s, threshold=3?** Floor = 5 × 3 = 15s (thực tế đo được
19.4s vì cộng 2s timeout của probe netblock và lệch pha poll).

**Muốn RTO 5 phút thì được chọn interval bao nhiêu?** Các phần khác của RTO đo được ≈ 9.1s
(restore 0.5s + warm-up 6.6s + TTL 2.0s). Detection xấu nhất ≈ (threshold + 1) × (interval + timeout)
= 4 × (interval + 2). Điều kiện 4 × (interval + 2) + 9.1 ≤ 300 → **interval ≤ ~70s**.
Trên thực tế nên chọn ≤ 30s để còn biên cho operator confirm thủ công và restore index lớn hơn.

**Reflection 1 — giảm thành phần nào mà không tăng flapping?** DNS/LB TTL (2.0s) và GPU warm-up
(6.6s, bằng hot standby): không ảnh hưởng tới quyết định failover nên không gây flapping;
cái giá là tải DNS cao hơn và chi phí GPU chạy không ở region B. Giảm detection mới là thứ đánh đổi với flapping.

**Reflection 2 — health checker chạy chung process với serving?** Khi process chết thì không còn
ai báo động. `dr/health_checker.py` chỉ import `argparse/json/pathlib/time/httpx`, không import
gì từ `serving/`, và chạy thành process riêng, probe qua HTTP từ bên ngoài.

**Reflection 3 — "RTO 5 phút có thật không?"** Mở `reports/drill-2-withdr.jsonl` (đồng hồ của user)
cùng `chaos/chaos-events.jsonl:3`, rồi chạy
`python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`
→ `rto_measured_s = 28.5`, có `path:line` cho mọi mốc trong `reports/rto-evidence.md`.
