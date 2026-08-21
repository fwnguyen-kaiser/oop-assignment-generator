# Báo cáo Kỹ thuật: Pipeline Sinh Bài Tập OOP Tự Động (LogicalPlan Generation)

---

## 1. Xác định vấn đề (Problem Definition)

### 1.1 Bài toán gốc
Hệ thống cần sinh tự động bài tập lập trình OOP (Java) dựa trên 2 input:
- **Domain config**: chủ đề, mô tả, từ khoá gợi ý entity/relationship (ví dụ: Banking, RPG Game).
- **Blueprint preset**: ràng buộc cấu trúc theo độ khó (số lượng class, độ sâu kế thừa, bật/tắt các đặc tính OOP như interface, abstraction, composition, aggregation).

Output cuối cùng: bộ file Java hoàn chỉnh (đáp án) + bộ file skeleton (đề bài cho sinh viên) + class diagram + tài liệu đề bài.

### 1.2 Vấn đề ban đầu (Pain point)
Cách tiếp cận đầu tiên: 1 LLM call duy nhất, nhận toàn bộ domain + preset, yêu cầu output trực tiếp ra `LogicalPlan` (đồ thị OOP hoàn chỉnh, đã gán sẵn abstract/interface/inheritance...) thông qua 1 prompt rất dài, nhồi nhiều chỉ thị kiểu "MUST", "WARNING", "CREATIVITY REQUIRED".

**Triệu chứng thất bại quan sát được**: LLM liên tục fail — không tuân thủ đồng thời được cả 2 loại ràng buộc khác bản chất nhau:
- Ràng buộc **sáng tạo/ngữ nghĩa**: tên entity, mô tả, quan hệ domain hợp lý.
- Ràng buộc **cấu trúc/số học cứng**: đúng số lượng class trong khoảng min-max, đúng độ sâu kế thừa tối đa, không có cycle, đúng số interface yêu cầu.

### 1.3 Chẩn đoán nguyên nhân gốc
Phân tích cho thấy đây không phải vấn đề "prompt chưa đủ mạnh" mà là vấn đề **năng lực nền tảng của LLM**: mô hình ngôn ngữ giỏi suy luận domain tự do nhưng yếu ở việc giữ đúng invariant số học/cấu trúc xuyên suốt nhiều bước sinh nội dung. Ép 1 lần generate phải thoả mãn cả 2 loại ràng buộc dẫn đến trade-off sai có hệ thống — không phải lỗi ngẫu nhiên có thể sửa bằng câu chữ prompt mạnh hơn.

Thêm 2 rủi ro cụ thể được nhận diện sớm:
- **Semantic Forcing**: nếu áp cấu trúc (graph shape) trước rồi ép LLM gán tên domain vào, LLM có xu hướng "rationalize" — tạo ra thiết kế vô lý (ví dụ `Account inherits Transaction`) kèm lời giải thích nghe hợp lý, khiến lỗi khó bị phát hiện ở review sau này vì trông có vẻ chủ đích.
- **Non-observability**: lỗi cấu trúc (thiếu class, sai depth) dễ detect bằng thuật toán (đếm, BFS); lỗi ngữ nghĩa (quan hệ domain vô lý) hoàn toàn không có validator tự động nào bắt được — chỉ human review mới nhận ra.

---

## 2. Phân tích và đề xuất phương án (Approach Analysis)

### 2.1 Nguyên tắc thiết kế được chọn: tách trách nhiệm theo năng lực

| Thành phần | Chịu trách nhiệm | Lý do |
|---|---|---|
| LLM (Pass 1) | SEMANTIC — domain logic, đặt tên, quan hệ có ý nghĩa | LLM mạnh nhất ở suy luận ngôn ngữ/tri thức tự do |
| Code (Pass 2) | STRUCTURAL — đếm, giới hạn, tránh cycle, đúng invariant | Đây là bài toán thuật toán thuần, code làm chính xác 100%, không cần đoán |
| LLM (Pass 1b, có kiểm soát) | Bổ sung nội dung mới khi thiếu (chỉ khi cần sáng tạo thêm) | Không bao giờ được gọi để "tự sửa cấu trúc" |

### 2.2 Cơ sở lý thuyết / tiền lệ tham khảo
Tra cứu cho thấy pattern "LLM formalize (ngữ nghĩa) → symbolic layer validate/repair (cấu trúc) → feedback lỗi cụ thể quay lại LLM" đã được áp dụng trong các hệ thống production tương tự:
- **ATLAS** (2025) — framework sinh artifact kỹ thuật có cấu trúc cho model-driven engineering, đảm bảo validity theo schema/rule trong khi biến lỗi tầng cao hơn thành đối tượng có thể chẩn đoán trong luồng generation.
- **Logic-LM và các biến thể** (LINC, SAT-LM...) — LLM dịch nội dung tự nhiên thành biểu diễn hình thức, solver verify, lỗi cụ thể quay lại LLM để refine.

Điểm chung rút ra và áp dụng trực tiếp: không để LLM vừa sáng tạo vừa tự đảm bảo invariant cấu trúc trong 1 lần generate; feedback loop luôn là lỗi có định vị cụ thể, không phải "sai rồi tự sửa lại đi".

### 2.3 Lý do chọn Semantic-First thay vì Structure-First
Cân nhắc 2 hướng:
- **Structure-first**: code sinh graph shape trước (đúng số lượng, đúng depth), LLM chỉ gán tên vào — rủi ro cao nhất là Semantic Forcing (đã nêu ở 1.3).
- **Semantic-first**: LLM sinh sketch tự do theo domain trước, code repair/validate cấu trúc sau — quyết định cuối cùng của đồ án.

Lập luận chốt: sinh graph ngẫu nhiên đúng constraint (N node, depth D, M interface, acyclic) tưởng chừng là bài toán khó ("generate a DAG with exact constraints"), nhưng với N ≤ 10 thực chất chỉ là backtrack/loop đơn giản. Ngược lại, rủi ro semantic của structure-first (LLM tự rationalize thiết kế vô lý) **không có cách nào bù lại bằng code**, vì đó là vấn đề thuộc phạm trù ý nghĩa. → Trade "code Pass 2 phức tạp hơn 1 chút" lấy "loại bỏ hoàn toàn 1 lớp lỗi không thể tự động phát hiện".

### 2.4 Câu hỏi phụ: có cần LangGraph không?
Đánh giá riêng: pipeline này có luồng đi **cố định, do code quyết định** (không phải LLM tự chọn node tiếp theo), không cần persist/resume phức tạp, không có nhiều agent phối hợp qua lại không biết trước số vòng. → Không match với use-case LangGraph được thiết kế cho (agent tự quyết định routing động). Kết luận: dùng plain Python state machine (`while` loop + rule cố định) thay vì thêm framework — giảm overhead học/debug không cần thiết cho scope hiện tại.

---

## 3. Thiết kế giải pháp (Solution Design)

### 3.1 Kiến trúc tổng thể — 6 Phase, 2 Pass

```
PASS 1 — STRUCTURAL SKETCHING & NORMALIZATION
  Phase 1  : LLM Sketching        (DomainConfig + Preset) → SketchPlan (raw)
  Phase 2  : Structural Repair    SketchPlan (raw) → SketchPlan (repaired, deterministic)
  Phase 3  : Logical Compilation  SketchPlan (repaired) → LogicalPlan (entity-centric)
  Phase E  : AST Bootstrap        LogicalPlan → JavaClass AST (structure only, no primitives)

PASS 2 — DETAIL ENRICHMENT & RENDERING
  Phase 4  : AST Enrichment       AST (structure) + LLM → AST (structure + primitives/methods)
  Phase 5  : Rendering            AST (full) → Java files, skeleton files, diagrams, assignment.md
```

> **Cập nhật (sau session refactor Tier1/2/3 + phase-naming audit)**: numbering ở trên là snapshot tại thời điểm viết báo cáo, **không còn khớp code hiện tại**. Đã đổi thật trong code (không chỉ đổi tên gọi):
> - `Phase E` → đổi số thành **Phase 4** (không còn dùng chữ), file output đổi từ `phase_e_java_ast.json` → `phase4_ast_bootstrap.json`.
> - `Phase 4` (AST Enrichment) cũ → tách làm 2: **Phase 5a-i** (LLM sinh signature, không có body) và **Phase 5a-ii** (`ContentRepairPipeline` sửa signature trước, rồi mới xin LLM viết body — tránh phí generation cho method sẽ bị dedupe/rename) — lý do và bug thật tìm được khi tách xem thêm README của `oop-assignment-generator` phần Tier 1/2/3.
> - Có thêm **Phase 6** (Compile Verification Gate — `javac` thật, Tier 1 deterministic + Tier 2 LLM last-resort) chưa từng xuất hiện trong bảng 6-phase gốc ở trên — được thêm sau thời điểm viết báo cáo này.
> - `Phase 5` (Rendering) cũ vẫn còn, nhưng chạy SAU Phase 6, file output đổi `phase4_detailed_ast.json` → `phase6_detailed_ast.json` cho khớp đúng thứ tự thật.
>
> Numbering thật hiện tại: `1 → 1b/1c → 2 → 3 → 4 (AST Bootstrap) → 5a-i (signatures) → 5a-ii (content repair + body-fill + contract fulfillment) → 6 (compile gate) → rendering cuối (không đánh số Phase riêng, là step nội bộ 3g-3j trong detail_pipeline.py)`.

### 3.2 Schema qua từng Phase

**SketchPlan (Phase 1 output / Phase 2 input-output)** — lỏng, không ép số lượng chính xác:
```python
class SketchEntity(BaseModel):
    name: str
    kind: Literal["core", "supporting"]
    note: str
    is_abstract: bool
    is_interface: bool

class SketchRelationship(BaseModel):
    from_entity: str
    to_entity: str
    type: Literal["inheritance", "composition", "aggregation", "association", "implements"]

class SketchPlan(BaseModel):
    design_rationale: str
    entities: List[SketchEntity]
    relationships: List[SketchRelationship]
```

**LogicalPlan (Phase 3 output)** — entity-centric, mỗi class tự cầm danh sách quan hệ của nó:
```python
class SemanticEntity(BaseModel):
    name: str
    description: Optional[str]
    is_abstract: bool
    is_interface: bool
    inherits_from: Optional[str]
    implements: Optional[List[str]]
    composes_with: Optional[List[str]]
    associated_with: Optional[List[str]]

class LogicalPlan(BaseModel):
    design_decisions: List[str]
    domain_entities: List[SemanticEntity]
    support_entities: List[SemanticEntity]
```

### 3.3 Chi tiết thuật toán Structural Repair Pipeline (trái tim của hệ thống)

Chạy trong vòng lặp hội tụ `while not converged and iteration < 10`:

| Rule | Mục đích | Cơ chế |
|---|---|---|
| 2.0 | Chuẩn hoá | Dedupe entity trùng tên (case-insensitive, remap reference), drop self-loop, drop dangling reference |
| 2.1 | Ép inheritance thành forest | Mỗi child chỉ giữ 1 parent thật; parent thứ 2 trở đi → tạm chuyển `implements` (chờ 2.6 quyết định) thay vì huỷ ngay |
| 2.1b | Phá cycle | DFS 3 màu (White-Gray-Black) trên `parent_map`; cắt đúng 1 cạnh tại điểm phát hiện cycle |
| 2.3 | Giới hạn độ sâu | Tính depth từ gốc; node vượt `max_depth` được reattach vào ancestor gần nhất còn hợp lệ, không phải root |
| 2.2 | Giải xung đột transitive | Cạnh structural trỏ tới ancestor/descendant của chính nó (qua inheritance) bị drop |
| 2.7a | Aggregation fallback | Nếu preset cấm aggregation, mọi cạnh aggregation còn lại → ép thành composition |
| 2.7b | Composition acyclic | DFS phát hiện cycle ownership (A owns B, B owns A) → giáng cấp cạnh gây cycle thành association |
| 2.5 | Cắt bớt class dư | Scoring theo (kind, degree, có phải parent hay không) → drop node điểm cao nhất, lặp tới khi đạt max |
| 2.8 | Sửa disconnected component | Node core cô lập → ép nối association tới core khác; node supporting cô lập → drop |
| 2.6 | Derive interface (chạy 1 lần, sau vòng lặp) | `is_interface=True` nếu (đủ tín hiệu cấu trúc + tín hiệu ngữ nghĩa + không có state trong note + không có out-edge) HOẶC (có edge `implements` trỏ tới) |
| 2.6b | Derive abstract (chạy 1 lần, sau vòng lặp) | `is_abstract=True` nếu chưa là interface và có ≥2 con hoặc note gợi ý "abstract"/"base" |
| 2.10 | Dọn dẹp hậu-kỳ (chạy 1 lần) | Cạnh `implements` mà target hoá ra không phải interface → downgrade thành association; xoá mọi out-edge của node đã thành interface (interface không giữ state) |

**Nguyên tắc quan trọng**: 2.6/2.6b/2.10 chạy **sau** vòng lặp hội tụ, đúng 1 lần, không trigger lại 2.0–2.8 — đây là giới hạn kiến trúc đã biết và chấp nhận (không phải fixed-point tuyệt đối cho toàn bộ hệ rule), được document rõ ràng để tránh hiểu nhầm khi maintain về sau.

### 3.4 Xử lý thiếu/thừa số lượng — Pass 1b có kiểm soát
Nếu sau repair vẫn thiếu class so với `min`: gọi LLM lại **phạm vi hẹp** ("đây là graph hiện tại, thêm đúng N entity mới phù hợp domain"), merge kết quả rồi chạy lại toàn bộ repair pipeline. Giới hạn tối đa 3 lần thử, có cảnh báo tường minh nếu vẫn không đạt (domain quá hẹp so với yêu cầu preset) thay vì cố ép vô hạn.

### 3.5 Contract giao tiếp giữa các thành phần
Mọi lỗi/feedback truyền giữa Pass 2 (code) và Pass 1 (LLM) đều ở dạng **structured object**, không phải prose:
```json
{"step": "2.3_depth_exceeded", "node": "PoodleBreed", "detail": "depth=5 > max=3", "action": "reattached to Dog"}
```
Nguyên tắc: lỗi thuần cấu trúc code tự sửa im lặng (không tốn LLM call); chỉ lỗi cần sáng tạo nội dung mới (thiếu entity) mới quay lại LLM, kèm structured error cụ thể — không bao giờ dùng câu chung chung kiểu "sai rồi, tự sửa lại".

---

## 4. Triển khai và các vòng lặp sửa lỗi (Implementation & Iteration Log)

Quá trình triển khai đi qua nhiều vòng review độc lập ("Independent Audit"), mỗi vòng phát hiện và vá các lớp lỗi khác nhau — phản ánh đúng tinh thần "structural bug dễ bắt bằng test, semantic bug cần review có chủ đích":

### 4.1 Nhóm lỗi hạ tầng (infra/hygiene)
- **API key bị lộ trong lịch sử lệnh** khi set trực tiếp qua command line — xử lý: rotate key ngay, chuyển sang `.env` + `python-dotenv`.
- **Retry cho lỗi mạng/API** ban đầu hoàn toàn thiếu (1 lần fail là toàn pipeline fail) → bổ sung `tenacity.retry` với exponential backoff cho toàn bộ 4 hàm gọi LLM.
- **`response.parsed is None`** (structured output parse fail) ban đầu không được check, dẫn tới lỗi `AttributeError` mù mờ → thêm check tường minh, raise kèm raw text để dễ debug.

### 4.2 Nhóm lỗi mutation & state
- **Input Pass 1 bị mutate bởi Pass 2**: do dùng chung reference thay vì copy, khiến file `phase1_sketch_plan.json` (đáng lẽ là snapshot trước-sửa) bị nhiễm luôn kết quả sau-sửa, làm mất giá trị so sánh trước/sau. → Fix: `sketch.model_copy(deep=True)` ngay đầu hàm `repair()`.

### 4.3 Nhóm lỗi thuật toán đồ thị (structural core)
- **Cycle nhiều node trong inheritance không bị phát hiện**: check ban đầu chỉ bắt được "1 child có 2 parent trực tiếp", không bắt được cycle gián tiếp (A→B→C→A) vì mỗi node là 1 key riêng trong map, không bao giờ va chạm nhau. → Fix: DFS 3 màu (White-Gray-Black) đúng chuẩn thuật toán phát hiện cycle trên đồ thị có hướng.
- **Xung đột inheritance–structural chỉ check cạnh trực tiếp**, bỏ sót quan hệ ông–cháu (transitive ancestor 2+ tầng) → Fix: hàm `ancestors()` duyệt ngược toàn bộ `parent_map`, không chỉ 1 tầng.
- **Composition cycle** (A owns B, B owns A) ban đầu hoàn toàn chưa được xử lý dù đã ghi trong thiết kế → bổ sung DFS riêng cho tập cạnh composition.
- **Dedupe entity trùng tên (case-insensitive) không remap reference**: sau khi dedupe `BankAccount`/`Bankaccount` về 1 bản, các relationship trỏ vào bản bị loại (khác hoa/thường) bị hiểu nhầm là dangling reference và bị xoá oan → Fix: xây `name_to_canonical` map, remap toàn bộ relationship trước khi check dangling.
- **Convergence loop không có safety net**: vòng lặp thoát sau 10 iteration dù chưa thật sự ổn định, không log cảnh báo → bổ sung `logger.warning` khi thoát vòng mà `converged=False`.

### 4.4 Nhóm lỗi ngữ nghĩa (semantic — khó phát hiện nhất, không có validator tự động)
- **`Bank composition Customer` quá chặt / `Customer association BankAccount` quá lỏng** so với ý nghĩa domain thật (Customer nên là chủ sở hữu mạnh hơn của Account so với Bank) — ghi nhận là quan sát semantic, không phải bug code, minh hoạ đúng luận điểm "lỗi loại này không thể tự động bắt bằng validator".
- **Derive `is_interface`/`is_abstract` chỉ dựa vào child-count thuần tuý** — bug nghiêm trọng nhất về mặt thiết kế: khiến `Account` (2 con, nhưng note mô tả rõ có "balance" — 1 thuộc tính, không thể là interface trong Java) bị ép thành interface chỉ vì hình dạng đồ thị. Đây là chính loại lỗi "structure ép semantic" mà toàn bộ kiến trúc 2-pass được sinh ra để né — nó tái phát ngay ở đúng bước tưởng chừng đã an toàn (Pass 2, code thuần). → Fix: thêm gate ngữ nghĩa (`is_semantic_interface` dựa trên `kind`/tên gọi, `has_state_keywords` dựa trên nội dung `note`) trước khi cho phép derive.
- **Node "abstract/interface" bị mark vẫn có thể giữ `composes_with`** — vi phạm ngữ nghĩa Java (interface không giữ instance field) vì bước derive (2.6) chạy sau khi `structural_edges` đã cố định → Fix: bổ sung Rule 2.10, strip mọi out-edge có state của node vừa được mark interface.
- **Cơ chế "multi-implements" tự triệt tiêu chính nó**: sau khi sửa 2.1 để tạm giữ cạnh dư dưới dạng `implements` (thay vì huỷ luôn), bước đếm tín hiệu ở 2.6 lại quên không đếm các cạnh `implements` này — khiến node target gần như không bao giờ đủ điều kiện thành interface, dẫn tới 2.10 downgrade ngược lại thành `association` ngay sau đó. Đây là bug rất khó phát hiện bằng test thông thường (không crash, kết quả "trông hợp lệ") → chỉ lộ ra qua trace tay từng bước theo đúng use-case mà cơ chế được thiết kế để phục vụ.

### 4.5 Nhóm lỗi schema/type
- `SketchRelationship.type` (Pydantic Literal) ban đầu không có giá trị `"implements"` dù code runtime gán giá trị này — không crash ngay (Pydantic v2 mặc định không validate lại khi mutate attribute), nhưng là quả bom hẹn giờ cho bất kỳ chỗ nào reconstruct object từ dict/JSON sau này → cần bổ sung `"implements"` vào Literal.

### 4.6 Nhóm gap chưa đóng hoàn toàn (open items)
- **`SemanticValidator`** (module kiểm tra `LogicalPlan` theo policy "depth phải bằng chính xác max_depth") hiện là dead code, chưa được wire vào pipeline chính — và nếu wire lại sẽ mâu thuẫn trực tiếp với `2.3` (chỉ trim khi vượt, không ép tăng khi thiếu). Cần chốt policy thống nhất (`max_depth` là ceiling hay target chính xác) trước khi kích hoạt lại.
- ~~**Liên kết Phase E ↔ Phase 4**: `run_detail_pipeline` đọc từ `output/phase_e_java_ast.json`, nhưng chưa xác nhận được `JavaBuilder.build_and_save()` có ghi ra đúng file này hay không — thiếu code `java_builder.py` để audit dứt điểm.~~ **[RESOLVED]** Đã confirm liên kết đúng (grep trực tiếp cả 2 file), và nhân tiện sửa luôn tên cho nhất quán: `Phase E` → `Phase 4`, file đổi thành `phase4_ast_bootstrap.json`, cả `java_builder.py` (nơi ghi) lẫn `detail_pipeline.py` (nơi đọc) đã đồng bộ.
- **Phase 1b không re-verify count sau vòng repair thứ 2**: nếu repair lần 2 lại làm giảm entity (do 2.5/2.8 trigger), không có bước kiểm tra lại `min_classes` lần cuối trước khi đi tiếp sang Phase 3.

---

## 5. Kiểm thử (Testing & Verification Strategy)

Thiết kế test theo 4 tầng, tương ứng với 4 loại lỗi khác nhau đã gặp:

### 5.1 Tầng 1 — Unit test cho từng rule (deterministic, input thủ công)
Nguyên tắc: mỗi rule cần ít nhất 1 test "trigger đúng" + 1 test "không trigger nhầm" (false positive). Đây là tầng bắt được phần lớn bug thật trong quá trình audit (cycle detection, mutation safety, dedupe remap...) — vì input dễ dựng tay, kết quả có đúng-sai rõ ràng duy nhất, không cần đoán.

### 5.2 Tầng 2 — Property-based test (Hypothesis)
Thay vì liệt kê case thủ công, generate random sketch rồi assert bất biến phải luôn đúng, bất kể input: không có cycle inheritance trong output, mỗi child ≤ 1 true-parent, không mất/nhân đôi entity ngoài ý muốn. Đây là cách hiệu quả để bắt edge-case mà người viết code không tự nghĩ ra.

### 5.3 Tầng 3 — Test cho phần có LLM (không deterministic)
- Schema-level test chạy thật (CI nightly, tốn API): assert sketch luôn pass `validate_sketch` (không dangling reference), assert entity count không lệch quá xa so với soft guidance.
- Mock LLM hoàn toàn cho test hằng ngày: test orchestration logic (retry loop, error handling, file writing) mà không tốn tiền/thời gian, không flaky.

### 5.4 Tầng 4 — Regression fixture từ lỗi thật đã gặp
Mỗi lần audit tìm ra sketch output kỳ lạ (structural hoặc semantic), lưu lại làm fixture cố định + test case riêng — hàng rào vĩnh viễn chống tái phát, đặc biệt quan trọng cho lớp lỗi semantic vốn không có validator tự động.

### 5.5 Bài học về việc "tin log pass"
Một sự cố đáng ghi nhận trong quá trình: báo cáo "test pass hết" dựa trên log thực tế nhưng khi trace tay logic của code thật tại thời điểm đó, phát hiện 1 test case (`test_dedupe_remaps_relationship_references`) **không thể pass** với code hiện có — cho thấy code đang chạy khác với code được audit (đã có bản vá chưa share). Bài học: xác nhận bằng trace tay/chạy trực tiếp quan trọng hơn tin tưởng báo cáo kết quả gián tiếp, đặc biệt với hệ thống có nhiều phiên bản code thay đổi nhanh.

---

## 6. Kết quả và đánh giá (Results)

### 6.1 Đạt được
- Kiến trúc 2-pass (Sketch → Repair) hoạt động đúng nguyên lý thiết kế: tách rõ trách nhiệm semantic (LLM) và structural (code), giảm về gần 0 khả năng LLM phải tự đồng thời tuân thủ 2 loại ràng buộc khác bản chất.
- Repair pipeline xử lý đầy đủ 11 rule (2.0 → 2.10), bao phủ toàn bộ case trong bảng đối chiếu thiết kế ban đầu: cycle (inheritance lẫn composition), depth vượt ngưỡng, thừa/thiếu class, xung đột transitive, disconnected component, derive interface/abstract có gate ngữ nghĩa, multi-implements.
- Convergence loop có giới hạn vòng lặp + cảnh báo tường minh khi không hội tụ — tránh silent-fail.
- Có retry/backoff cho tầng gọi LLM, có structured error contract giữa các pass.
- Tài liệu đặc tả kỹ thuật (V3) đã được đối chiếu và xác nhận khớp với code thật ở toàn bộ 6 phase.

### 6.2 Rủi ro còn tồn tại
- Lớp lỗi semantic (quan hệ domain hợp lý nhưng không "đúng" theo trực giác — ví dụ độ mạnh-yếu của composition/association) vẫn ngoài khả năng phát hiện tự động của hệ thống — cố hữu với cách tiếp cận semantic-first, chỉ giảm thiểu được qua domain hint tốt hơn (gợi ý interface có tên cụ thể thay vì để code tự derive từ hình dạng đồ thị) và human review định kỳ, không loại bỏ hoàn toàn được.
- 3 open item ở mục 4.6 cần đóng trước khi coi pipeline là production-ready: policy `max_depth` thống nhất, ~~xác nhận liên kết file Phase E↔4~~ **[đã resolve]**, re-verify count sau Phase 1b vòng 2.

### 6.3 Định hướng tiếp theo
- Đóng 3 open item còn treo.
- Bổ sung domain hint có tên interface cụ thể (thay vì chỉ optional entity chung chung) để giảm phụ thuộc vào cơ chế derive-từ-cấu-trúc.
- Cân nhắc bổ sung tầng 4 (regression fixture) một cách hệ thống hơn khi hệ thống chạy với nhiều domain thật, thay vì chỉ dựa vào audit thủ công như giai đoạn hiện tại.
