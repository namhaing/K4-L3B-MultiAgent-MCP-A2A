# L3B Architecture Record

Mô tả kiến trúc của bản nộp **v6** (commit `b091fdb`, nhánh `l3b-solution`, điểm public 92.01).
Tài liệu chỉ ghi các quyết định kiểm chứng được trong code; không chứa prompt, chain-of-thought hay API key.
Runtime hoàn toàn **rule-based, deterministic**, không dùng LLM.

## 1. System overview

Mỗi case được xử lý độc lập bởi một coordinator và các agent chuyên trách. Agent là hàm Python có
`actor` riêng; mọi phối hợp giữa chúng được ghi thành sự kiện quan sát được trong `traces/trace.jsonl`.

```text
case input ──▶ coordinator ──task_assigned──▶ entity-agent ──(customer history ∥ order)──▶ handoff
                   │
                   ├──task_assigned──▶ order-agent    ─┐
                   ├──task_assigned──▶ payment-agent  ─┤  gọi song song (một vòng MCP)
                   ├──task_assigned──▶ shipment-agent ─┤  mỗi kết quả → tool_result_consumed
                   ├──task_assigned──▶ policy-agent   ─┘  rồi handoff về coordinator
                   │
                   ├── quyết định issue ──▶ (order-agent: get_sellers nếu seller chịu trách nhiệm)
                   ├── policy-agent: policy_decided (áp rule của policy cho issue)
                   ├── conflict-resolver: policy_decided cho từng xung đột nguồn
                   └── verifier: kiểm tra bất biến → verification_completed → handoff
                                   │
                                   ▼
                        outputs/<case_id>.json + trace (case_received … case_finalized)
```

Code chính: `workflow.py` (coordinator và agents), `analysis.py` (logic nghiệp vụ thuần, không gọi MCP),
`case_context.py` (cache, sổ evidence, quyền tool, trace theo case), `gateway_compat.py` (kết nối MCP),
`cli.py` (chạy batch, reconnect, `--resume`). `mcp_gateway.py` giữ nguyên như starter.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer (`entity-agent`) | `claimed_order_id`, `candidate_order_ids`, `customer_unique_id_hint` | Lấy lịch sử khách hàng, xác minh order, loại candidate giả | `get_customer_history`, `get_order` | `handoff` RESOLVED/NOT_FOUND kèm evidence refs |
| Coordinator (`coordinator`) | Input case, kết quả các agent | Giao việc, chọn phiên bản order, quyết định issue, tổng hợp output | Không gọi tool | `task_assigned` cho từng agent; output cuối |
| Order/product (`order-agent`) | Order đã resolve | Items, seller, giá trị đơn; xác minh seller khi seller chịu trách nhiệm | `get_order_items`, `get_sellers`, `get_product_context` | `handoff` ITEMS_LOADED / SELLER_VERIFIED |
| Shipment (`shipment-agent`) | Order, phiên bản đã chọn | Timeline giao hàng, sự kiện `delivered_late`, bên gây trễ | `get_shipment_summary`, `get_sellers` | `handoff` ON_TIME / SELLER_DELAY / LOGISTICS_DELAY |
| Payment/refund (`payment-agent`) | Order, phiên bản đã chọn | Capture, trùng tiền, split, mismatch, refund pending/failed | `get_payment_timeline`, `get_order_payments`, `get_refund_timeline` | `handoff` với verdict thanh toán |
| Policy (`policy-agent`) | `policy_version`, issue | Lấy bảng rule `EC_POLICY_V2`, áp `case_status`, action, refund, bên chịu trách nhiệm | `get_policy` | `policy_decided` + `handoff` |
| Conflict resolver (`conflict-resolver`) | Dòng `get_order` và phiên bản đã chọn | Ghi xung đột nguồn và nguồn được chọn | Không gọi tool | `policy_decided` cho mỗi xung đột + `handoff` |
| Verifier (`verifier`) | Output nháp, sổ evidence của case | Kiểm tra và sửa bất biến trước khi finalize | Không gọi tool | `verification_completed` (PASS/FIXED) + `handoff` |

Least privilege được kiểm tra trong `CaseContext.call()`: actor gọi tool ngoài danh sách của mình sẽ bị
từ chối (`PermissionError`). Tool discovery (`list_tools`) chỉ dùng để kiểm tra gateway, không mở quyền.
Các lookup độc lập (items, payment, refund, shipment, policy) chạy đồng thời bằng `asyncio.gather`;
số call và evidence giống hệt khi chạy tuần tự.

## 3. Entity resolution và A2A protocol

**Resolve order.** Candidate phải là order ID 32 ký tự hex; placeholder như `candidate-NNN` bị loại
mà không gọi tool. Thứ tự thử: `claimed_order_id` rồi các candidate còn lại; một candidate không phải
claimed chỉ được thử nếu có trong lịch sử khách hàng. Candidate đầu tiên mà `get_order` trả dữ liệu hợp lệ
là `resolved` (confidence 0.95); mọi candidate khác vào `rejected_candidates`. Không có candidate hợp lệ →
`not_found` → output fallback `insufficient_evidence` (confidence 0.4), không suy diễn tiếp.

**Chọn phiên bản dữ liệu.** Lịch sử khách hàng trả nhiều bản ghi cho cùng một order (nguồn mâu thuẫn).
Bản ghi được chọn là bản mới nhất có ngày mua **và** ngày giao dự kiến không sau `opened_at` (vấn đề phải
quan sát được lúc khiếu nại); nếu không có thì bản mới nhất mua trước `opened_at`; cuối cùng là bản sớm nhất.
Sự kiện payment/refund thuộc phiên bản có ngày mua gần nhất trước sự kiện; `delivered_late` thuộc phiên bản
giao đúng ngày đó; sự kiện lặp y hệt giữa các phiên bản trùng thời điểm chỉ tính một lần.

**Message envelope.** Mọi message giữa agent là một trace event theo `trace-event-v1`: `case_id`, `actor`,
`target`, `decision_code`, `tool_name`, `evidence_refs` (tối đa 20) và `attributes` gồm `run_id` (tương quan
cả lần chạy) và `seq` (thứ tự message trong case). Correlation theo `case_id` + `run_id` + `seq`.

**Handoff, timeout, tránh vòng lặp.** Specialist chỉ `handoff` sau khi kết quả đã gắn vào findings; coordinator
chỉ quyết định issue khi mọi handoff đã về. Pipeline cố định, agent không gọi ngược nhau. Mỗi `(tool, tham số)`
được gọi tối đa một lần mỗi case (cache), trần 10 call/case. Read timeout HTTP 60 giây.

## 4. Evidence và conflict lifecycle

1. **Validate response.** `CompatGateway.call` đọc cờ lỗi và structured content (tương thích mcp v1/v2),
   hoặc một text block JSON duy nhất, rồi validate envelope theo `mcp-evidence-response-v1`. Tool báo lỗi
   (ví dụ order không có refund event) → không có evidence, không retry.
2. **Lưu ref.** `CaseContext` tạo mới cho từng case một sổ `evidence_ref → {tool, domain, actor, data}`.
   Ref không bao giờ được tạo, sửa, ghi ra đĩa để dùng lại, hay dùng chéo case/lần chạy.
3. **Emit.** Ngay sau mỗi call thành công: `tool_result_consumed` với `tool_name`, `evidence_refs=[ref]`,
   `attributes.domain`. Handoff của specialist mang lại các ref mà chính agent đó thu được.
4. **Chọn nguồn.** Rule nghiệp vụ lấy từ `get_policy(EC_POLICY_V2)`: `case_status`, `recommended_action`,
   `refund_brl`, `responsible_parties` theo issue; `party_id` của seller lấy từ dữ liệu của case.
   Xung đột giữa dòng `get_order` và phiên bản được chọn (`order_status`, `order_purchase_timestamp`) được
   ghi vào `data_conflicts` với `selected_source = get_customer_history`, `resolution_code =
   LATEST_RECORD_BEFORE_CASE_OPENED`, kèm một `policy_decided` của conflict-resolver cho mỗi xung đột.
5. **Map vào output.** `evidence_refs` là các ref trong sổ của case, bỏ những domain không chứng minh kết
   luận (case giao trễ không trích payment, vì payment chỉ dùng để tính tổng tiền). `claim_assessments` trích
   cùng tập ref. Verifier loại mọi ref không có trong sổ của case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi mạng trong một call | 1 retry tại chỗ | Vẫn lỗi → CLI mở lại phiên MCP và làm lại case từ đầu (tối đa 8 lần liên tiếp, chờ `min(30, 3n)` giây); quá giới hạn thì dừng, chạy tiếp bằng `day09 run --resume` | Trace của case bị đệm và chỉ ghi khi case hoàn tất, nên không có event dở dang; cảnh báo `WARN` ra stderr |
| Tool trả lỗi (`is_error`) | 0 | Không có evidence cho tool đó; case tiếp tục với evidence còn lại | Không có `tool_result_consumed` cho call đó |
| Entity not found/ambiguous | 0 | Output `insufficient_evidence`, `needs_investigation`, confidence 0.4 | `verification_completed` = `FALLBACK_ENTITY_NOT_FOUND` |
| Source conflict | 0 (rule tất định) | Chọn phiên bản theo `opened_at`, ghi `data_conflicts` | `policy_decided` = `LATEST_RECORD_BEFORE_CASE_OPENED` |
| Invalid specialist result / exception / output sai schema | 0 | Output fallback hợp lệ schema, chỉ dùng evidence đã thu | `verification_completed` = `FALLBACK_<Exception>` |

**Query budget và cache.** Mỗi `(tool, tham số)` gọi tối đa một lần mỗi case, trần 10 call/case. Tool chỉ gọi
khi cần: `get_refund_timeline` chỉ cho claim về refund; `get_shipment_summary` chỉ khi trễ theo ngày hoặc claim
về giao hàng; `get_sellers` chỉ khi seller chịu trách nhiệm; không gọi `get_product_context` và
`get_order_payments`. Trung bình v6: 5.6 call/case. Mọi tool là read-only nên retry idempotent; thiếu evidence
thì trả fallback, không bao giờ đoán dữ liệu. `--resume` giữ các case đã hoàn tất và làm lại case fallback.

## 6. Verification invariants

Verifier chạy trên output nháp, sửa theo rule nếu vi phạm, rồi emit `verification_completed`
(`PASS` hoặc `FIXED`, kèm số lần sửa và số MCP call của case).

- **Schema:** output được validate theo `l3b-output-v2` ngay trong `solve_case`; không hợp lệ → fallback
  hợp lệ. CLI validate lại lần nữa và kiểm tra `case_id` trước khi ghi file.
- **Entity scope:** `resolved_order_ids ∩ rejected_candidates = ∅`; `affected_entities` chỉ chứa order đã
  resolve cùng item/seller của phiên bản được chọn.
- **Evidence ownership:** mọi ref trong output thuộc sổ evidence của chính case; ref trong
  `claim_assessments` là tập con của `evidence_refs`; mỗi ref đều có `tool_result_consumed` tương ứng.
- **Timeline:** phiên bản được chọn phải quan sát được tại `opened_at`; `timeline_complete` chỉ đúng khi đủ
  5 mốc (mua, duyệt, bàn giao carrier, giao khách, dự kiến).
- **Payment/refund totals:** `recommended_refund_brl` = tổng `refund_lines` và không vượt
  `refundable_total_brl` (captured − refunded); `case_status = no_action` ⇒ refund 0, không có refund line.
- **Source precedence:** rule từ policy; xung đột nguồn ghi đủ `sources`, `selected_source`, `resolution_code`.
- **Responsibility/action consistency:** `late_seller_ids` chỉ có khi issue là `late_delivery_seller`;
  giao trễ do vận chuyển thì không quy trách nhiệm cho seller; action lấy từ policy, không trùng, tối đa 8.
- **Confidence bounds:** 0.9 khi evidence chỉ ủng hộ đúng issue khách nêu; 0.75 khi evidence ủng hộ nhiều
  issue; 0.6 khi kết luận khác claim; ≤ 0.5 khi thiếu rule policy; 0.4 cho fallback; entity 0.95.

## 7. Reproducibility

- **Model/config:** không dùng LLM; mọi quyết định là rule tất định trên evidence và policy `EC_POLICY_V2`.
  Giá trị ngẫu nhiên duy nhất là định danh (`event_id`, `run_id`), không ảnh hưởng kết quả.
- **Môi trường:** Python ≥ 3.11 (đã chạy với 3.12). Dependency theo `pyproject.toml`: `httpx2` 2.x,
  `jsonschema[format]` 4.x, `mcp` 2.x, `python-dotenv` 1.x.
- **Concurrency:** các case chạy tuần tự; trong một case tối đa 5 lookup MCP chạy đồng thời.
- **Lệnh chạy:**

  ```bash
  python -m pip install -e ".[dev]"
  cp .env.example .env              # điền COMPETITION_TEAM_API_KEY của team
  day09 validate-inputs
  day09 run                         # hoặc: day09 run --resume để chạy tiếp lần chạy bị ngắt
  day09 validate
  day09 package --output dist/submission.zip
  ```

- **Tài nguyên:** khoảng 5.6 MCP call/case (khoảng 560 call cho 100 case). Thời gian chạy phụ thuộc độ trễ
  của gateway (đã gặp từ khoảng 3 đến 15 phút cho 100 case).
- **Debug tùy chọn:** biến môi trường `L3B_DEBUG_DIR` lưu dữ liệu evidence từng case ra thư mục cục bộ để
  kiểm tra offline; thư mục này không bao giờ được đóng gói vào bài nộp.
- Không ghi API key vào code, trace, output hay tài liệu.
