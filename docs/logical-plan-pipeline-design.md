# LogicalPlan Generation Pipeline — Sketch → Repair Design

## 0. Nguyên tắc tổng

- **Pass 1 (LLM)**: chịu trách nhiệm SEMANTIC. Không biết, không quan tâm min/max/depth chính xác.
- **Pass 2 (code, deterministic)**: chịu trách nhiệm STRUCTURAL. Không tự nghĩ tên/domain, chỉ sửa hình dạng graph.
- **Pass 1b (LLM, targeted)**: chỉ được gọi lại khi Pass 2 cần THÊM nội dung mới (thêm entity), không bao giờ được gọi để "tự sửa cấu trúc".
- Vòng lặp có **circuit breaker**: tối đa N lần repair-loop, quá thì fallback (dùng template gần nhất hoặc trả lỗi tường minh cho user/dev, không cố "ép cho ra" vô hạn).

---

## 1. Pass 1 — LLM Sketch Generation

### Input
- Domain config (name, description, keywords, entity_hints, relationship_hints) — dùng nguyên, không strip.
- Preset numbers — đưa vào prompt dạng **soft guidance**, không dạng "MUST":
  > "Hệ thống hướng tới khoảng {min}-{max} class, độ sâu kế thừa khoảng {max_depth}. Đây là ước lượng tham khảo, không phải yêu cầu chính xác — cứ thiết kế theo domain logic trước."

### Output schema (SketchPlan) — cố tình lỏng hơn LogicalPlan

```json
{
  "entities": [
    {"name": "Account", "kind": "core", "note": "..."},
    {"name": "SavingsAccount", "kind": "supporting", "note": "..."}
  ],
  "relationships": [
    {"from": "SavingsAccount", "to": "Account", "type": "inheritance"},
    {"from": "Account", "to": "Transaction", "type": "composition"}
  ],
  "design_rationale": "..."
}
```

Điểm khác biệt quan trọng so với LogicalPlan cuối:
- Không có field `depth`, `interface_flag` bắt buộc — Pass 2 tự suy ra.
- `type` relationship chỉ cần 1 trong 4 giá trị chuẩn hoá: `inheritance | composition | aggregation | association`. LLM không cần biết interface/abstract ở bước này — đó là quyết định cấu trúc, để Pass 2 hoặc Pass 1b quyết sau.
- Không giới hạn số lượng entity/relationship — sketch có thể lệch preset khá xa, đó là **kỳ vọng bình thường**, không phải lỗi.

### Validation tối thiểu ngay tại Pass 1 (schema-level, không phải structural-level)
- Mọi `from`/`to` phải reference tên có trong `entities` (dangling reference → reject, retry Pass 1 với lỗi cụ thể, KHÔNG sang Pass 2).
- Dùng `response_schema` native của provider thay vì nhồi JSON schema vào prompt.

---

## 2. Pass 2 — Structural Repair Pipeline (pure code, không LLM)

### 2.0 Chuẩn hoá đầu vào
1. **Dedupe entity theo tên** (case-insensitive, fuzzy match threshold ví dụ Levenshtein ≤ 2 hoặc embedding similarity) — LLM dễ tạo `Bank` và `BankEntity` là cùng 1 ý.
2. **Tách relationship thành 2 tập riêng biệt ngay từ đầu**:
   - `inheritance_edges` — sẽ ép thành **forest** (mỗi node ≤ 1 parent, vì Java single inheritance).
   - `structural_edges` — composition / aggregation / association, xử lý như graph thường (cho phép nhiều edge/node).
3. Loại bỏ self-loop (`A -> A`) — log warning, drop thẳng.

### 2.1 Xử lý inheritance — ép về forest hợp lệ
- Nếu 1 node có **> 1 parent** trong sketch (LLM lỡ đề xuất multiple inheritance kiểu class, không phải interface): giữ lại parent có "note"/rationale mạnh nhất (hoặc theo thứ tự xuất hiện đầu tiên), các edge còn lại **downgrade thành `association`** thay vì xoá thẳng — tránh mất thông tin domain hoàn toàn.
- Vì bản chất đây đã là forest sau bước trên → **cycle trong inheritance là không thể xảy ra về mặt cấu trúc**. Không cần thuật toán cycle-detection phức tạp ở đây.

### 2.2 Xử lý conflict giữa inheritance và structural edge (case "Account inherits Transaction nhưng Transaction lại compose Account")
- Rule ưu tiên: **inheritance thắng, structural edge trùng cặp (A,B) bị drop** — vì 1 cặp class không thể vừa là "is-a" vừa là "has-a" cùng chiều.
- Nếu structural edge đó là **chiều ngược** (`B -> A` thay vì `A -> B`) và inheritance là `A -> B`: đây thực ra hợp lệ về mặt UML (class con có thể sở hữu tham chiếu tới thứ khác), **giữ nguyên, không phải conflict**. Chỉ conflict khi trùng cả cặp lẫn 1 trong 2 chiều tạo vòng sở hữu vô nghĩa (`A extends B` và `A has-a B` cùng lúc — sở hữu chính cha của mình).
- Check ancestor/descendant trước khi add mỗi structural edge (O(depth), N ≤ 10 nên rẻ):
  ```
  can_add_structural(a, b, forest):
      if a in ancestors(b, forest) or b in ancestors(a, forest):
          reject / downgrade to association
      else:
          accept
  ```

### 2.3 Depth vượt max
- BFS/DFS tính depth từ mọi root.
- Với node vi phạm (depth > max_depth):
  - **Reattach vào ancestor gần nhất còn hợp lệ**, không phải root — giữ tối đa ngữ nghĩa: `new_parent = ancestor_at_depth(node, max_depth - 1)`.
  - Nếu reattach làm node đó "is-a" một class không còn hợp domain logic (ví dụ node lá quá đặc thù), **ưu tiên phương án 2.4 (convert relation) hơn là ép reattach vô lý** — cụ thể: nếu depth vi phạm mà node có thể hiểu là "thuộc tính/trạng thái" (`is_specialization_by_state` heuristic: tên node dạng adjective/trạng thái) thì convert thành attribute/enum của parent thay vì giữ làm class riêng.

### 2.4 Thiếu class so với min
- Gọi lại LLM **targeted, phạm vi hẹp**:
  > "Đây là graph hiện tại: {sketch tóm tắt}. Domain: {...}. Đề xuất thêm đúng {N} entity mới phù hợp domain, mỗi entity kèm loại quan hệ dự kiến với 1 entity đã có."
- Response merge lại → chạy lại từ bước 2.0 (không skip validation vì content mới có thể tạo conflict mới).
- **Giới hạn số vòng gọi thêm** (ví dụ tối đa 2 lần) để tránh loop vô hạn nếu domain quá hẹp không đủ ý tưởng (domain "Loài Chó" mà preset đòi min 8 class chẳng hạn) — hết vòng mà vẫn thiếu → hạ `min` xuống mức đạt được thực tế + log cảnh báo, KHÔNG cố nhồi entity vô nghĩa.

### 2.5 Thừa class so với max
- Scoring để chọn node **merge/drop**, ưu tiên theo thứ tự:
  1. Leaf node (không có con) + không tham gia structural edge nào (orphan) → drop thẳng.
  2. Leaf node có degree thấp nhất (ít quan hệ nhất) → merge vào parent bằng cách chuyển thành attribute (`SavingsAccount.interestRate` thay vì class riêng, nếu node chỉ mang 1-2 note đơn giản).
  3. Không bao giờ drop node có `kind: core` từ domain_hints nếu còn node `optional`/LLM-tự-thêm để drop thay thế.
- Sau mỗi lần drop, re-check disconnected component (xem 2.7).

### 2.6 Thiếu/thừa interface dù `interface.enabled = true/false`
- Interface không tồn tại sẵn trong sketch (Pass 1 không được yêu cầu nghĩ về nó) → đây là bước **derive**, không phải "sửa lỗi LLM":
  - Điều kiện 1 node đủ tư cách thành interface: có **≥ 2 con trực tiếp trong inheritance forest** (điểm chung hành vi) HOẶC được note là abstract concept trong domain_hints.
  - Chọn ứng viên theo branching factor cao nhất trước, convert đúng số lượng cần (thường preset chỉ cần "có ít nhất 1", không có max cứng thường gặp — nếu preset có max thì cap lại).
  - Nếu `enabled = false` mà sketch/derive vô tình tạo ra cấu trúc giống interface (1 node abstract nhiều con) → **không sao**, đây là aggregation của abstraction feature (xem 2.6b), chỉ literally đổi keyword `interface` → `abstract class` khi serialize.

### 2.6b Abstraction (`abstraction.enabled`)
- Node được chọn làm abstract phải **có ít nhất 1 con** — validate bắt buộc, nếu chọn nhầm node lá thì loại, chọn lại.
- Nếu `enabled = false`: mọi node đang đứng vai "chỉ tồn tại để làm cha, không bao giờ tự đứng độc lập" phải được flatten hoặc gán thành concrete class bình thường (bỏ đánh dấu abstract, không xoá node).

### 2.7 Composition vs Aggregation
- Nếu `aggregation.enabled = false`: mọi structural edge được LLM gắn `aggregation` → **force convert thành `composition`** (strict ownership), không drop — vì mất thông tin quan hệ thì tệ hơn là làm chặt nghĩa hơn 1 chút.
- Composition edge cũng nên acyclic theo hướng ownership (A owns B, B không được own ngược lại A) — check tương tự 2.2 nhưng riêng cho tập composition.

### 2.8 Disconnected component check (chưa liệt kê trước đó nhưng bắt buộc)
- Sau tất cả bước trên, chạy 1 lần connectivity check trên toàn graph (union tất cả loại edge).
- Node nào cô lập hoàn toàn (không inheritance, không structural edge nào) → 2 lựa chọn:
  - Nếu domain_hints liệt kê nó ở `core` → bắt buộc gắn ít nhất 1 edge hợp lý nhất (association tới node core gần nghĩa nhất, so bằng embedding/keyword overlap).
  - Nếu không → drop, coi như "ý tưởng phụ LLM tự thêm nhưng không tích hợp được".

### 2.9 Convergence guard
- Mỗi bước sửa (2.1 → 2.8) tạo ra 1 object lỗi có structure (không phải string prose):
  ```json
  {"step": "depth_violation", "node": "PoodleBreed", "detail": "depth=5 > max=3", "action": "reattached_to=Dog"}
  ```
- Toàn bộ list action này log lại — vừa để debug, vừa optionally feed ngược cho LLM ở bước cuối để nó viết `design_decisions` giải thích (chỉ mang tính mô tả, không ảnh hưởng cấu trúc nữa).
- Nếu sau 1 pass sửa mà bước nào đó lại tạo vi phạm mới (ví dụ merge node ở 2.5 làm graph disconnected) → chạy lại toàn bộ pipeline tối đa 3 vòng. Vòng 4 vẫn fail → escalate: trả về lỗi rõ ràng kèm full log thay vì cố ép ra kết quả sai.

---

## 3. Case table tổng hợp (bao gồm case bạn nêu + case bổ sung)

| Case | Xử lý ở bước |
|---|---|
| Cycle ngữ nghĩa (A inherit B, B compose A) | 2.2 |
| Inheritance quá sâu so với domain (banking depth 5) | 2.3 |
| Thiếu class vs min | 2.4 (targeted LLM call, có giới hạn vòng) |
| Thừa class vs max | 2.5 |
| Interface enabled nhưng sketch không có ứng viên | 2.6 |
| Interface disabled nhưng cấu trúc tự nhiên giống interface | 2.6 |
| Abstract class không có con | 2.6b |
| Aggregation disabled nhưng LLM đề xuất aggregation | 2.7 |
| Multiple inheritance (Java không hỗ trợ) | 2.1 |
| Self-loop | 2.0 |
| Duplicate/trùng tên entity (fuzzy) | 2.0 |
| Dangling reference (relationship trỏ tới entity không tồn tại) | Pass 1 validation, reject sớm |
| Node cô lập hoàn toàn | 2.8 |
| Domain quá hẹp không đủ ý cho min class | 2.4 fallback (hạ min + log) |
| Pipeline lặp không hội tụ | 2.9 circuit breaker |

---

## 4. Contract giữa 2 Pass — luôn structured, không bao giờ prose

Feedback (cả trong repair loop lẫn khi cần gọi LLM lại) luôn ở dạng:
```json
{"errors": [{"code": "DEPTH_EXCEEDED", "node": "...", "constraint": 3, "actual": 5}]}
```
không phải câu văn "Lỗi rồi, sửa lại đi cho đúng". LLM chỉ nhận structured error khi việc sửa **cần sáng tạo nội dung mới** (case 2.4) — mọi lỗi thuần cấu trúc (2.1, 2.2, 2.3, 2.5, 2.6, 2.7, 2.8) code tự sửa, không quay lại LLM.
