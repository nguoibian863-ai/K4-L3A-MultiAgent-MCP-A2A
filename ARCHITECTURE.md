# L3A Architecture Record

Tài liệu này ghi lại kiến trúc và các quyết định kỹ thuật của hệ thống Multi-Agent L3A. Mọi quyết định đều có thể kiểm chứng độc lập thông qua mã nguồn, trace log và public contracts.

## 1. System overview

Luồng xử lý từ hồ sơ khiếu nại đầu vào đến kết quả thẩm định và nhật ký giám sát:

```text
[Input Case] ──► [Coordinator] ──► [Specialist Agents] ──► [Verifier] ──► [Output JSON]
                        │                    │                 │
                        └──── Trace Log ◄────┴── MCP Gateway ──┘
```

1. **Input**: Tải từ `inputs/<case_id>.json`.
2. **Coordinator**: Tiếp nhận hồ sơ, lập kế hoạch điều tra và phân công nhiệm vụ cho các chuyên gia qua sự kiện `task_assigned`.
3. **Specialist Agents**: Gọi các MCP tools theo đúng thẩm quyền được cấp, tiêu thụ dữ liệu và phát sinh mã chứng cứ `evidence_ref` gắn với sự kiện `tool_result_consumed`. Sau đó bàn giao qua `handoff`.
4. **Verifier**: Rà soát các bất biến nghiệp vụ (invariants), đối chiếu bằng chứng, tổng hợp kết luận, tính toán bồi hoàn tài chính và phát sinh sự kiện `verification_completed`.
5. **Output & Trace**: Xuất file `outputs/<case_id>.json` tuân thủ 100% `day09-l3a-output-v2` và ghi vết toàn bộ vào `traces/trace.jsonl`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool Permissions | Output/handoff |
| --- | --- | --- | --- | --- |
| **Coordinator** | `case_id`, `customer_request`, `policy_version` | Khởi tạo điều tra, phân tích phạm vi khiếu nại, kích hoạt active run | Không gọi MCP tools | Phân công nhiệm vụ (`task_assigned`) tới các Specialists |
| **Order/item** | `case_id`, `claimed_order_id` | Xác minh trạng thái đơn hàng (`canceled`, `unavailable`, `delivered`), danh mục sản phẩm, người bán liên quan | `get_order`, `get_order_items` | `ORDER_ANALYSIS_COMPLETED` kèm danh sách item_ids, seller_ids, order status |
| **Payment** | `case_id`, `claimed_order_id` | Đối soát lịch sử thanh toán, phương thức trả tiền (split, credit), phát hiện duplicate charge hoặc refund failed/pending | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `PAYMENT_ANALYSIS_COMPLETED` kèm tổng tiền thanh toán, payment_references, trạng thái hoàn tiền |
| **Shipment** | `case_id`, `claimed_order_id` | Phân tích timeline giao nhận, so sánh ngày giao thực tế với cam kết để phân định trễ hạn do bên vận chuyển hay người bán | `get_shipment_summary` | `SHIPMENT_ANALYSIS_COMPLETED` kèm chỉ dấu `late_seller` hoặc `late_logistics` |
| **Policy** | `case_id`, `policy_version` | Tra cứu điều khoản bồi thường, mức trần hoàn tiền BRL và bên chịu trách nhiệm tương ứng theo quy định sàn | `get_policy` | `POLICY_ANALYSIS_COMPLETED` kèm bộ quy tắc nghiệp vụ `rules` |
| **Verifier** | Case gốc và kết quả từ 4 Specialists | Kiểm tra toàn vẹn Invariants, phân loại `primary_issue`, tính toán `financial_resolution`, đánh giá claims, hoàn thiện output | Không gọi MCP tools (chỉ kiểm định dữ liệu từ specialists) | Final Output JSON (`outputs/<case_id>.json`) |

## 3. A2A protocol

- **Message Envelope & Correlation**: Mọi tương tác và dữ liệu luân chuyển đều được gắn chặt với khóa `case_id`.
- **Handoff Contract**: Các Specialist Agent hoàn tất phân tích sẽ phát sự kiện `handoff` với `actor=<specialist_name>`, `target="verifier"` và `decision_code` tương ứng.
- **Tránh lặp (Acyclic Directed Flow)**: Luồng điều phối đi theo một chiều xác định: `Coordinator -> Parallel Specialists -> Verifier`. Không có cơ chế vòng lặp phản hồi đệ quy giữa các agent nhằm loại trừ hoàn toàn nguy cơ deadlock hoặc loop vô tận.
- **Trace Boundaries**: Chỉ phát các sự kiện vòng đời quan sát được (`case_received`, `task_assigned`, `tool_result_consumed`, `handoff`, `verification_completed`, `case_finalized`). Tuyệt đối không ghi prompt thô hoặc chuỗi suy luận nội bộ (chain-of-thought).

## 4. Evidence lifecycle

- **Validation**: Mọi phản hồi từ MCP Server đều được kiểm tra hợp lệ tức thì qua `mcp-evidence-response-v1.schema.json`.
- **Isolation (Chống nhiễm chéo)**: Mỗi `evidence_ref` sinh ra được khóa chặt trong phạm vi của đúng `case_id` đang xử lý. Hệ thống không lưu trữ cache dùng chung hay chuyển giao evidence giữa các case khác nhau.
- **Consumption Trace**: Mỗi khi Specialist Agent sử dụng dữ liệu từ tool để đưa vào kết luận, một sự kiện `tool_result_consumed` được phát ra ngay lập tức với danh sách `evidence_refs=[evidence_ref]`.
- **Claim Linkage**: Tất cả `evidence_ref` trong `claim_assessments` và `evidence_refs` của output đều là tập con của các evidence đã được audit từ MCP Gateway.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| **MCP timeout** | Tối đa 2 lần với exponential backoff | Ghi nhận thiếu dữ liệu cho domain đó, chuyển cho Verifier đánh giá mức độ thiếu | `decision_code="MCP_TIMEOUT_FALLBACK"` |
| **Not found (404/Empty)** | Không retry | Đặt trạng thái domain tương ứng là rỗng (`None`/`[]`), không suy diễn thông tin | `decision_code="ENTITY_NOT_FOUND"` |
| **Source conflict** | Không retry | Nếu phát hiện mâu thuẫn giữa các nguồn (ví dụ order status vs shipment event), Verifier ưu tiên nguồn có thẩm quyền cao hơn và ghi vào `data_conflicts` | `decision_code="CONFLICT_RESOLVED"` |
| **Tool Execution Error** | Retry 1 lần sau khi xác nhận active run | Ghi nhận lỗi domain, Verifier kết luận `insufficient_evidence` nếu thiếu dữ liệu trọng yếu | `decision_code="TOOL_ERROR_FALLBACK"` |

## 6. Verification invariants

Trước khi sinh file output cuối cùng, Verifier Agent bắt buộc thực thi 7 phép kiểm định bất biến:

1. **Schema Compliance**: Đảm bảo cấu trúc tuân thủ 100% `day09-l3a-output-v2.schema.json` (không có thêm bất kỳ trường lạ nào ngoài schema).
2. **Entity Scope**: Các ID trong `affected_entities` (order, item, seller, payment) phải thuộc về case hiện tại, không chứa dữ liệu giả lập.
3. **Evidence Authenticity**: Mọi mã `evidence_refs` đưa vào output phải có tiền tố `ev_` và được cấp từ MCP Gateway trong phiên chạy hiện tại.
4. **Consistency**:
   - Nếu `case_status == "no_action"`, thì `recommended_refund_brl == 0.0` và `refund_lines == []`.
   - Nếu `case_status == "action_required"` và có hoàn tiền, thì `recommended_refund_brl` phải bằng tổng `amount_brl` của các dòng trong `refund_lines`.
   - `primary_issue` phải nhất quán với `responsible_parties` (ví dụ: `late_delivery_seller` phải gắn với seller, `late_delivery_logistics` gắn với logistics_provider).
5. **Currency**: Đơn vị tiền tệ bắt buộc phải là `"BRL"`.
6. **Confidence Bounds**: Giá trị `confidence` nằm trong khoảng `[0.0, 1.0]`, được hiệu chuẩn phù hợp với độ tin cậy của bằng chứng.
7. **Action Scope**: Số lượng `resolution_actions` tối đa 8 phần tử, không chứa chuỗi rỗng.

## 7. Reproducibility

- **Runtime**: Python 3.12+ trên hệ điều hành Windows.
- **Dependencies**: `httpx2>=2,<3`, `jsonschema>=4.25,<5`, `mcp>=2,<3`, `python-dotenv>=1.1,<2`.
- **Command Line**:
  - Chạy toàn bộ case: `day09 run`
  - Kiểm tra tính hợp lệ: `day09 validate`
  - Đóng gói bài nộp: `day09 package --output dist/submission.zip`
- **Security**: Không lưu thông tin nhạy cảm, API keys hay secrets vào repo hoặc file nộp bài.
