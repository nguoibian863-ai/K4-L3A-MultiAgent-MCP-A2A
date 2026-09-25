# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input → Coordinator → Order/Item Agent → Payment Agent → Shipment Agent → Policy Agent → Verifier → Output
                              │                 │                │              │
                              └──────────────── MCP Evidence Gateway ───────────┴── Trace
```

Pipeline gần như tuyến tính, không chạy song song: mỗi case đi qua 5 agent chuyên trách theo thứ tự cố định, với đúng một nhánh điều kiện (quay lại payment-agent lấy refund timeline khi các tín hiệu khác chưa đủ kết luận — xem mục 3). Coordinator không tự gọi MCP tool nào — vai trò của nó chỉ là điều phối (`task_assigned`) và đóng khung vòng đời case (`case_received`/`case_finalized` được `cli.py` phát ra trước và sau khi gọi `solve_case()`).

Toàn bộ logic nằm trong `src/student_agent/workflow.py`, hàm `solve_case()`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | `case` (customer_request, policy_version) | Phát `task_assigned` cho từng specialist theo đúng thứ tự; không gọi MCP tool nào | Điều phối tuyến tính order → payment → shipment → policy → verifier |
| Order/item agent | `claimed_order_id` | Gọi `get_order`, `get_order_items`, `get_sellers` (hồ sơ seller có thẩm quyền, dùng làm evidence khi seller chịu trách nhiệm); khử trùng lặp bản ghi item xung đột (`_canonical_items`: chọn bản ghi có `shipping_limit_date` sớm nhất làm nguồn chính, ghi `data_conflicts` khi `freight_value` giữa các bản ghi khác nhau) | Bằng chứng order + item chuẩn hoá → handoff sang payment-agent |
| Payment agent | order_id, order evidence | Gọi `get_order_payments`, `get_payment_timeline`. Chỉ dùng event nằm trong **cửa sổ vòng đời đơn**: event thanh toán trong 24h kể từ lúc mua, event refund trong khoảng [ngày mua, max(ngày giao, ngày dự kiến) + 7 ngày]. Trong cửa sổ đó: `reconciliation_mismatch` → payment mismatch; ≥2 lần `captured` cùng số tiền mà tổng các lần lặp **không** bằng giá trị đơn, và tổng đã thu vượt giá trị đơn → duplicate charge (các khoản bằng nhau cộng lại đúng giá trị đơn là các phần của 1 lần trả chia nhỏ, không phải thu trùng); ≥2 lần `captured` có tổng đúng bằng giá trị đơn → valid split payment. **`get_refund_timeline` chỉ được gọi khi các tín hiệu khác không kết luận được** (xem mục 3): `refund_requested` gần nhất pending/failed → refund pending/failed, còn lại → unsupported claim | Tín hiệu payment → handoff sang shipment-agent; ở vòng refund → handoff sang policy-agent |
| Shipment agent | order_id | Gọi `get_shipment_summary`; xác định trễ giao **bằng cách so sánh `delivered_customer_at` với `estimated_delivery_at`**. Event `delivered_late` chỉ được dùng để quy trách nhiệm (field `actor`) khi nó nằm trong ±1 ngày quanh ngày giao thực tế — event lệch xa ngày giao bị coi là nhiễu | Có kết luận → handoff sang policy-agent; chưa kết luận được → handoff lại payment-agent để kiểm tra refund |
| Policy agent | `primary_issue` đã được suy luận từ evidence + `policy_version` | Gọi `get_policy`; lấy `case_status`, `recommended_action`, `refund_brl`, loại bên chịu trách nhiệm từ rule khớp `primary_issue`. `party_id` của seller trong rule là giá trị mẫu dùng chung cho mọi case, nên policy-agent **gắn lại bằng `seller_id` thật của đơn** lấy từ evidence `get_order_items` (`_bind_parties`) | `policy_decided` (kèm case_status, refund_brl, loại bên chịu trách nhiệm) → handoff sang verifier |
| Verifier | Output nháp + tập evidence_ref đã consumed | Không gọi thêm MCP tool; chạy các kiểm tra chéo ở mục 6 (`_verify`), rồi tính confidence (`_confidence`). Phát `verification_completed` với `decision_code=verified` hoặc `flagged` và `attributes` ghi số check fail, tên check fail, confidence. Lớp bảo vệ cuối cùng là `Contracts.validate_output()` trong `cli.py` | `outputs/<case_id>.json` |

**Tool permission theo actor** (không agent nào được gọi tool ngoài phạm vi của mình):
- order-agent: `get_order`, `get_order_items`, `get_sellers`
- payment-agent: `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`
- shipment-agent: `get_shipment_summary`
- policy-agent: `get_policy`
- coordinator, verifier: không gọi MCP tool nào (verifier chỉ suy luận trên evidence đã có, tránh gọi lại tool trùng lặp)

## 3. A2A protocol

- Mỗi bước trong quá trình phối hợp được ghi lại dưới dạng một trace event duy nhất—thuộc các loại task_assigned, handoff, tool_result_consumed, policy_decided, hoặc verification_completed—và phải tuân thủ chuẩn trace-event-v1.schema.json. Ở đây, không tồn tại một "message payload" tách rời; chính các trace event này đóng vai trò là envelope (bao thư) duy nhất có thể quan sát được.
- **Correlation**: mọi event và mọi lời gọi MCP đều mang `case_id` bắt buộc; `gateway.call()` yêu cầu `case_id` ở mọi lời gọi nên evidence không thể lẫn giữa các case.
- **Điều kiện handoff**: order-agent → payment-agent → shipment-agent luôn theo thứ tự cố định. Sau shipment-agent có đúng một nhánh điều kiện: nếu trạng thái đơn, thanh toán, giao hàng đã đủ để kết luận (canceled/unavailable, mismatch, duplicate, trễ giao, split) thì handoff thẳng sang policy-agent; nếu chưa thì handoff lại payment-agent để lấy refund timeline **một lần**, rồi sang policy-agent. Nhánh này chỉ đi tối đa 1 lần nên không tạo vòng lặp. Lý do: với đơn không có refund, `get_refund_timeline` trả về lỗi và mọi lời gọi đều bị audit, nên chỉ gọi khi kết quả có thể thay đổi quyết định (giảm số lời gọi lỗi từ 60 xuống 10 case).
- **Timeout**: dựa vào timeout của HTTP client bên dưới (`httpx2.Timeout(300s, connect=30s)`, cấu hình tại `connect_gateway()` trong `mcp_gateway.py`). Không có timeout riêng ở tầng agent.
- **Tránh vòng lặp**: pipeline không có cơ chế "quay lại agent trước" — nếu 1 bước thất bại, toàn bộ case rẽ thẳng sang trạng thái `insufficient_evidence` thay vì retry giữa các agent.

## 4. Evidence lifecycle

1. Mọi response từ MCP đều được `EvidenceGateway.call()` validate qua `mcp-evidence-response-v1.schema.json` trước khi trả về (`contracts.validate_evidence`) — dữ liệu sai schema sẽ raise lỗi ngay tại nguồn.
2. `evidence_ref` lấy trực tiếp từ response, không bao giờ tự sinh hay chỉnh sửa.
3. Mỗi lần agent tiêu thụ 1 evidence để ra quyết định, `trace.emit(event_type="tool_result_consumed", evidence_refs=[...])` được gọi ngay sau lời gọi tool tương ứng — mapping 1-1 giữa tool call và trace event.
4. Agent vẫn gọi đủ tool để loại trừ các khả năng khác, nhưng `evidence_refs` ở output chỉ trích evidence liên quan tới kết luận (`_RELEVANT_EVIDENCE` trong `workflow.py`). Mọi case đều trích nhóm cốt lõi định nghĩa đơn hàng và dòng tiền: **order, items, payment_timeline, policy**. Thêm theo loại lỗi:

   | primary_issue | Evidence thêm ngoài nhóm cốt lõi |
   | --- | --- |
   | canceled_order_paid | — |
   | unavailable_order_paid | sellers (seller chịu trách nhiệm) |
   | late_delivery_seller | shipment, sellers |
   | late_delivery_logistics | shipment |
   | payment_mismatch, duplicate_charge, valid_split_payment | payments |
   | refund_pending, refund_failed | refund_timeline |
   | unsupported_claim | shipment (chứng minh giao đúng hạn) |

   Không trích evidence chỉ chứa nhiễu cho kết luận (ví dụ refund_timeline ở case không liên quan hoàn tiền, shipment ở case thuần thanh toán), và không bao giờ trích domain customer/product. Lý do trích rộng nhóm cốt lõi: thiếu nhóm evidence bắt buộc là hard gate (0 điểm cả case), còn trích thừa chỉ giảm nhẹ precision.

   Không tái sử dụng evidence giữa các case vì mỗi lời gọi gateway đều truyền `case_id` của chính case đang xử lý.
5. `claim_assessments[].evidence_refs` trích cùng tập evidence ở bước 4; verifier kiểm tra tập này là tập con của `evidence_refs` top-level.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP lỗi/timeout khi lấy `get_order`/`get_order_items`/`get_order_payments`/`get_payment_timeline`/`get_shipment_summary`/`get_policy` | Không retry (1 lần duy nhất/case, để tránh nhân đôi audit call) | Toàn bộ case rơi vào `insufficient_evidence`, `case_status=needs_investigation`, không suy đoán số liệu | `handoff` coordinator→verifier, `verification_completed` với `decision_code=insufficient_evidence` |
| `get_refund_timeline` "not found" (đơn không có lịch sử refund) | Không cần retry — đây là kết quả hợp lệ, không phải lỗi | Tool chỉ được gọi khi còn phải phân biệt refund pending/failed với unsupported claim; lỗi "not found" được hiểu là không có refund → `unsupported_claim` | Không emit `tool_result_consumed` (không có evidence_ref); handoff payment-agent → policy-agent vẫn được ghi |
| Source conflict (2 bản ghi item cùng `order_item_id` nhưng khác `freight_value`) | Không áp dụng (không phải lỗi runtime) | Chọn bản ghi có `shipping_limit_date` sớm nhất làm nguồn chính (`resolution_code=earliest_shipping_limit_selected`) | Ghi vào `data_conflicts[]` của output, không có trace event riêng |
| Event timeline nằm ngoài vòng đời đơn (trước ngày mua, hoặc cách xa ngày mua/ngày giao nhiều tuần) | Không áp dụng | Bỏ qua event đó khi suy luận; không coi là bằng chứng cho `primary_issue` | Không có trace event riêng; evidence_ref của tool vẫn được trích vì phần còn lại của response được dùng |
| Invalid specialist result (vd. `primary_issue` suy ra không khớp key nào trong `rules` của `get_policy`) | Không retry | Case rơi vào `insufficient_evidence` giống hàng đầu tiên (bắt bằng `KeyError` trong cùng khối try/except) | `verification_completed` với `decision_code=insufficient_evidence` |

Nguyên tắc chung: retry bị giới hạn ở mức 0 (fail-fast) vì mọi lời gọi MCP đều bị audit — retry vô tội vạ sẽ tốn quyền lợi của team mà không cải thiện độ chính xác. Khi thiếu evidence, hệ thống không bao giờ tự đoán số liệu để lấp đầy output.

## 6. Verification invariants

Verifier (`_verify` trong `workflow.py`) chạy các kiểm tra sau trên output nháp; mỗi check fail được ghi tên vào `attributes.failed_checks` của event `verification_completed`:

- **Money totals** (`refund_lines_total_mismatch`): tổng `refund_lines[].amount_brl` phải bằng `recommended_refund_brl`.
- **Status/refund** (`refund_without_action`): `case_status=no_action` thì không được có tiền hoàn.
- **Action từ policy** (`action_not_from_policy`): `resolution_actions[0]` phải đúng `recommended_action` của rule.
- **Root cause** (`root_cause_mismatch`): cause hạng 1 phải khớp `primary_issue`.
- **Seller responsibility** (`seller_out_of_scope`): mọi bên chịu trách nhiệm loại seller phải nằm trong `affected_entities.seller_ids` (seller thật của đơn).
- **Evidence ownership** (`evidence_not_consumed`): mọi `evidence_ref` ở output phải thuộc tập đã `tool_result_consumed` trong chính case này, và không rỗng.
- **Claim linkage** (`claim_evidence_unlinked`): evidence của từng claim phải là tập con của `evidence_refs` top-level.

Ngoài ra `cli.py` còn chạy `Contracts.validate_output()` (schema) và đối chiếu `case_id` output với input trước khi ghi file.

**Confidence calibration** (`_confidence`), luôn nằm trong [0.05, 0.99]:
- Điểm gốc 0.97 cho mọi loại lỗi. Bảng điểm public cho thấy calibration (sau khi bỏ hệ số chung) khớp đúng với trường hợp 100% `primary_issue` đúng, nên confidence thấp hơn chỉ làm mất điểm calibration mà không phản ánh rủi ro thật.
- −0.15 nếu số tiền hoàn của policy không khớp với số tiền nào trong event thuộc vòng đời đơn (không được evidence chứng thực).
- −0.15 nếu trễ giao nhưng không có event `delivered_late` hợp lệ để xác định bên chịu trách nhiệm.
- −0.3 cho mỗi check verifier bị fail.
- Không trừ điểm vì `data_conflicts`: các xung đột item đã được giải quyết theo quy tắc cố định và không làm đổi kết luận.
- `insufficient_evidence` cố định 0.1.

## 7. Reproducibility

- **Model/config**: hệ thống không dùng LLM, hoàn toàn là rule-based deterministic logic trong `workflow.py` — không có prompt hay chain-of-thought cần ẩn.
- **Dependency pinning**: xem `pyproject.toml` (`mcp>=2,<3`, `httpx2>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`).
- **Concurrency**: `cli.py` xử lý các case tuần tự (vòng `for case_id in case_set.case_ids`), không có concurrency limit riêng vì không chạy song song.
- **Random seed**: không dùng ngẫu nhiên ở bất kỳ đâu trong workflow — cùng 1 input + cùng 1 trạng thái MCP luôn cho cùng 1 output.
- **Lệnh chạy**: `day09 run` rồi `day09 validate`; đóng gói bằng `day09 package --output dist/submission.zip`.
- **Giới hạn tài nguyên**: mỗi case gọi 6 MCP tool (order, items, sellers, payments, payment-timeline, shipment) cộng 1 lần `get_policy`; `get_refund_timeline` chỉ gọi thêm khi cần (khoảng 30/100 case). Không có vòng lặp gọi lại, tối đa 8 call/case.
- **Vệ sinh audit**: mọi lời gọi MCP (kể cả thử nghiệm) đều bị audit và được tính cho lần nộp kế tiếp. Không chạy probe/test trên các case giữa lượt `day09 run` cuối và lúc upload.
