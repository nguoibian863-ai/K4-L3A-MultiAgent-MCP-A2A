# L3A architecture

## Luồng xử lý

`Coordinator → Order / Payment / Shipment / Policy specialists → Verifier → Output`

- `workflow.py` điều phối bốn specialist bất đồng bộ. Mỗi specialist nhận nhiệm vụ,
  lấy evidence, ghi `tool_result_consumed`, rồi bàn giao refs và số lỗi cho verifier.
- `analysis.py` phân tích dữ liệu độc lập với network. Không dùng tên case, thứ tự case
  hoặc bảng đáp án để quyết định. Claim chỉ xác định phạm vi cần điều tra; một claim
  chỉ được chọn khi có điều kiện nghiệp vụ độc lập hỗ trợ.
- `cli.py` tạo run một lần bằng URL trong cấu hình, kiểm tra HTTP status, chạy tối đa
  hai case đồng thời. Mỗi case có MCP session và trace riêng. Nếu transport ngắt,
  kết nối lại tối đa hai lần; chỉ trace của lần hoàn tất được đưa vào artifact.

## Công cụ và bằng chứng

| Specialist | Công cụ | Vai trò |
| --- | --- | --- |
| Order | `get_order`, `get_order_items`, khi cần `get_sellers` | Trạng thái, item, seller thuộc đơn |
| Payment | `get_payment_timeline`; fallback `get_order_payments`; khi cần `get_refund_timeline` | Capture, reconciliation, duplicate, refund lifecycle |
| Shipment | `get_shipment_summary` | Thời điểm bàn giao, giao nhận, trách nhiệm từ sự kiện |
| Policy | `get_policy` | Status, action, refund và loại bên chịu trách nhiệm |
| Verifier | Không gọi network | Phân tích kết quả specialist và tạo output |

Gateway khám phá tool và kiểm tra arguments theo schema trước khi gọi. Mọi phản hồi
được kiểm tra public evidence schema. Cache giữ phản hồi thành công trong một lần xử
lý case, kể cả khi reconnect; khóa gồm case_id, tool và arguments. Case mới hoặc run
mới tạo cache mới. Không đọc cache từ artifact cũ.

Payment timeline đã chứa payment rows nên không gọi thêm `get_order_payments` khi
timeline thành công. Refund chỉ được truy vấn khi claim hoặc dữ liệu có liên quan.
Shipment được gọi khi có claim giao nhận, claim không được hỗ trợ, phạm vi chưa rõ,
hoặc bằng chứng order/payment/refund chưa đủ kết luận. Nếu đã đủ bằng chứng để giải
quyết vấn đề thanh toán, specialist shipment bàn giao `NO_LOOKUP_NEEDED` và không gọi
tool. Không gọi customer/product tools cho các khiếu nại không liên quan.

## Quyết định nghiệp vụ

- Refund: đọc `data.events`, sắp xếp thời gian và xét trạng thái cuối. Phản hồi
  `{"events": []}` không chặn các nhánh khác. Refund hoàn tất không tạo hoàn tiền lần hai.
- Payment mismatch: dùng sự kiện reconciliation có thẩm quyền.
- Duplicate: dùng sự kiện duplicate trực tiếp, hoặc nhiều capture bằng nhau với tổng
  thực thu vượt giá trị đơn. Hai số tiền bằng nhau không đủ kết luận duplicate.
- Split payment: các payment sequence khác nhau và tổng capture khớp giá trị item + freight.
- Giao trễ: cần giao khách sau hạn hoặc sự kiện `delivered_late` đã xác nhận. Bàn giao
  trễ nhưng giao khách đúng hạn không tự động được tính là giao trễ.
- Seller: lấy ID từ item thuộc đơn, không sao chép ID cụ thể từ policy dùng chung.
- Policy quyết định số tiền, status và action. Tính tiền bằng `Decimal` rồi xuất JSON number.
- Claim yêu cầu hoàn toàn bộ được đánh giá riêng; bồi hoàn một phần là
  `partially_supported`, không tự động đồng nghĩa với hoàn toàn bộ.

Một số dữ liệu MCP có cùng item ID nhưng nhiều shipping limit và nhiều nhóm capture
ở các ngày khác nhau. Khi đồng thời có hai dấu hiệu này, ưu tiên nhóm capture khớp
ngày mua của order; nếu không xác định được mới dùng nhóm gần nhất trước `opened_at`.
Refund xuất hiện sau nhóm capture kế tiếp vẫn được giữ khi số tiền gắn duy nhất với
nhóm đang xét. Sự kiện giao hàng cũ chỉ được loại khi snapshot order và shipment
cùng xác nhận một thời điểm giao mới hơn. Mọi lựa chọn đều ghi `data_conflicts`.
Đây là heuristic xử lý dữ liệu xung đột, không phải
quy tắc được bảo đảm bởi public contract. Order snapshot nằm ngoài khoảng đang xét
không được dùng để khẳng định trạng thái; thiếu bằng chứng sẽ cần điều tra thêm.

## Lỗi và confidence

- Lỗi tool được ghi thành handoff `EVIDENCE_INCOMPLETE`; không coi lỗi là kết quả rỗng.
- Lỗi cấu trúc/khác order làm dừng xử lý thay vì âm thầm bỏ qua.
- Thiếu dữ liệu cốt lõi hoặc thiếu refund evidence cho claim refund:
  `insufficient_evidence`, `needs_investigation`, không tự cấp hoàn tiền.
- Confidence thay đổi theo bằng chứng trực tiếp, suy luận số tiền, xung đột và thiếu dữ liệu.
  Xung đột đã giải quyết bằng ngày mua/snapshot thống nhất có trần 0,92; xung đột chưa
  giải quyết giữ trần 0,80 hoặc 0,65. Warning từ MCP giữ trần 0,80.
  Đây là heuristic, chưa được calibration trên nhãn chuẩn độc lập.

## Artifacts và kiểm tra

```powershell
day09 run --artifacts-dir dist/my-run
day09 validate --artifacts-dir dist/my-run
day09 package --artifacts-dir dist/my-run --output dist/submission-improved.zip
```

Mỗi run ghi `outputs/`, `traces/trace.jsonl`, `case-traces/`, `evidence.jsonl`, `run.json`
trong thư mục riêng. Không ghi đè bản cũ. `evidence.jsonl` phục vụ debug cục bộ;
không chứa credential và không được đóng gói. ZIP chỉ có manifest, outputs và trace.

Validator kiểm tra schema, inventory, refund sum/status, seller scope, lifecycle trace,
output/claim refs đã được consume và không trùng giữa các case. Validator không thay
thế audit phía máy chủ và không tính được semantic score riêng của competition.

Tests dùng dữ liệu tổng hợp cho lỗi nghiệp vụ, isolation, tool discovery và trace.
`scripts/replay_evidence.py` cho phép chạy lại phân tích trên evidence đã lưu để debug;
script không tạo submission và không xem claim topic là ground truth.

`scripts/check_offline_workflow.py dist/ban-cai-tien` chạy cả workflow trên capture cũ,
kiểm tra schema/trace/nhất quán, so sánh quyết định khi gọi tool có chọn lọc với khi có
đủ bằng chứng, và đếm tool dự kiến. Script không truy cập mạng, không tạo ZIP nộp bài
và không tính điểm chính thức.
