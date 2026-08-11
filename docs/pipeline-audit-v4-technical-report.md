# Technical Audit Report — Assignment Generation Pipeline (v4)

Independent audit. Renumbered taxonomy, execution order, by phase. Every rule below is either read directly from current source (`file:line` cited) or verified by an actual reproduction (`javac` exit code, live LLM run, or `pytest`). No case in this report is speculative.

---

## 0. Pipeline shape (current)

```
PHASE 1  Semantic Sketching (LLM)              -> SketchPlan (raw)
PHASE 2  Structural Repair (pure code)         -> SketchPlan (repaired)
PHASE 3  Logical Compilation (pure code)       -> LogicalPlan
PHASE 4  AST Bootstrap (pure code)             -> JavaClass[] (structure only)
PHASE 5  AST Enrichment (LLM) + Content Repair -> JavaClass[] (fields/methods filled, deterministically cleaned)
PHASE 6  Compile Verification Gate             -> JavaClass[] (javac-verified)
PHASE 7  Rendering                             -> .java files, diagrams, assignment.md
```

Files: `src/pipeline.py` (1–3), `src/builders/java_builder.py` (4, 7.1), `src/detail_pipeline.py` (5–7 orchestration), `src/validator/repair_pipeline.py` (2), `src/validator/content_repair_pipeline.py` (5.2–5.9), `src/validator/compile_gate.py` (6), `src/llm/gemini.py` (all LLM calls), `src/builders/mermaid_builder.py` (7.2), `src/builders/markdown_builder.py` (7.3).

---

## PHASE 1 — Semantic Sketching (LLM)

### 1.1 Sketch Generation
`gemini.py:16` `generate_sketch()`. Structured output (`response_schema=SketchPlan`), retried via `tenacity` (5 attempts, exponential backoff 2–65s). Orchestration retry in `pipeline.py:119` (3 attempts, feeds `validate_sketch()` errors back into the next prompt as `previous_errors`).

`validate_sketch()` (`pipeline.py:11`): rejects any sketch with a dangling `from_entity`/`to_entity` — reject-and-retry, never passed to Phase 2.

**Prompt hard rule** (`gemini.py:62–64`, added this session): if the LLM sets `is_interface: true` on any entity, it MUST also emit an `implements` edge targeting it in the same response. Purpose: prevent orphan interfaces at the source instead of relying on 2.15 to catch them after the fact.

### 1.2 Entity Count Guarantee
`pipeline.py:22` `apply_min_classes_guarantee()`. Pure function — no file I/O, injectable `llm`/`repair_engine` (refactored out of the monolithic `run_pipeline()` this session specifically so it's unit-testable).

Loop, max 3 attempts: recompute `missing_count = min_classes - len(current_entities)` fresh each round (not cached), call `llm.generate_missing_entities()`, merge, re-run full Phase 2 (`repair_engine.repair()`). Exception in any attempt is caught and logged, loop continues to next attempt. If still short after 3 attempts: `[WARNING]` logged, sketch proceeds anyway with fewer than `min_classes`. No forced entity invention.

### 1.3 Interface Existence Guarantee
`pipeline.py:47` `apply_interface_guarantee()`. Same pure-function shape as 1.2. Triggers only if `preset.oop.interface.enabled and not any(is_interface)`.

Loop, max 2 attempts: `llm.generate_missing_interface()` — prompt explicitly forbids converting an existing entity (`gemini.py:136–139`: "Do NOT force any EXISTING entity to become an interface — that destroys its state/relationships"), requires proposing one *new* interface entity + an `implements` edge. Merge, re-run Phase 2. If Phase 2's rule 2.15 demotes the result (orphan, no real implementer survived), the loop's own `any(is_interface)` check at the top of the next iteration correctly sees `False` and retries. After 2 attempts: `[WARNING]` logged, ships without an interface — `markdown_builder` (7.3) will not falsely claim one exists (see 7.3).

**Case proven live**: `e_commerce.yaml` — before the 1.1 prompt hard-rule + domain YAML fix (below), this path was needed on every run. After: 0 invocations on 3 consecutive live re-runs (LLM produced a valid, non-orphan interface directly in 1.1).

---

## PHASE 2 — Structural Repair (`repair_pipeline.py`, pure code, no LLM)

Runs inside `repair(sketch)`. Rules 2.1–2.9 execute inside a convergence loop (`while not converged and iteration < 10`); 2.10–2.15 execute once, after the loop exits.

| # | Rule | Mechanism | Line |
|---|---|---|---|
| 2.1 | Normalization | Dedupe entities case-insensitive via `name_to_canonical` map; remap every relationship's `from_entity`/`to_entity` through that map *before* dangling-check (prevents case-variant dedupe from falsely orphaning references); drop self-loops (`from == to`) | 29–61 |
| 2.2 | Inheritance Forest Enforcement | First inheritance edge per child wins as `parent_map[child]`; every subsequent edge for the same child is downgraded `type = "implements"` and moved into `structural_edges` (not discarded — held for 2.11 to decide) | 70–83 |
| 2.3 | Cycle Breaking | DFS 3-color (White/Gray/Black) over `parent_map`; edge closing a Gray-to-Gray back-edge is cut, downgraded to `association` | 85–116 |
| 2.4 | Depth Constraint | BFS depth from computed `parent_map`; node exceeding `max_depth` reattached to `ancestor_at(max_depth - 1)`, not root | 118–156 |
| 2.5 | Transitive Conflict Resolution | For every structural edge `(u,v)`: if `v ∈ ancestors(u)` or `u ∈ ancestors(v)` (full ancestor walk, not just direct parent), drop the edge | 158–180 |
| 2.6 | Aggregation Fallback | If `preset.oop.aggregation.enabled` is falsy (including `None`, guarded), every surviving `aggregation` edge is force-converted to `composition` | 182–189 |
| 2.7 | Composition Acyclic Check | Separate DFS 3-color over composition-only edges; cycle edge downgraded to `association` | 191–220 |
| 2.8 | Excess Class Trimming | Scoring per candidate node: `+10` if `kind != core`, `+50` if degree 0, `+5` if degree 1, `+20` if not a parent in `parent_map`; drop highest score, one node per loop iteration, repeat until `len(entities) <= max` | 222–246 |
| 2.9 | Disconnected Component Repair | Node with empty adjacency: if `kind == core`, force an `association` edge to the nearest other core node (or any remaining node); else drop | 248–270 |
| 2.10 | Convergence Guard | Loop caps at 10 iterations; `logger.warning` + `print` if it exits without `converged == True` | 272–274 |
| 2.11 | Interface Derivation | `is_interface = True` iff `(count≥2 AND is_semantic_interface AND NOT has_state_keywords AND out_edges==0)` OR `(impl_count≥1 AND is_semantic_interface AND NOT has_state_keywords AND out_edges==0)`. `is_semantic_interface := kind=="supporting" OR name ends "able"/"ible"`. `has_state_keywords` := note contains any of `properties, balance, attributes, fields, state, core properties` | 290–313 |
| 2.12 | Abstract Derivation | `is_abstract = True` iff not already interface, and (`count≥2` OR note contains `"abstract"`/`"base"`) | 310–313 |
| 2.13 | Post-Interface Structural Cleanup | `implements` edge whose target is not (after 2.11) actually `is_interface` → downgrade to `association`. Any structural edge (`composition`/`aggregation`/`association`) sourced *from* a now-interface node → dropped (interfaces hold no state) | 315–330 |
| 2.14 | Interface-Extends-Class Cleanup | Inheritance edge where `from_entity.is_interface == True` and `to_entity.is_interface == False` → dropped (Java forbids `interface X extends ConcreteClass`) | 332–341 |
| 2.15 | Orphan Interface Demotion | Any entity with `is_interface == True` and zero `implements`-edges-into it and zero inheritance-children → `is_interface = False`. Covers *both* interfaces derived by 2.11 and interfaces set directly by the LLM in Phase 1 that never got wired to an implementer | 343–360 |

**Confirmed bugs this taxonomy closes** (all reproduced with `javac` or direct assertion before the fix landed):
- 2.14 — `interface X extends ConcreteY` (invalid Java; reproduced via crafted multi-inheritance-loser case).
- 2.15 — `PaymentProcessor`/`PaymentGateway` orphan interfaces shipped with zero implementers, live in `e_commerce.yaml` runs, both before 2.15 existed (case A) and after — via `impl_count` branch missing the `is_semantic_interface` gate (case B, the `Vehicle`-becomes-interface bug, fixed by adding the gate to *both* branches of 2.11's condition).

---

## PHASE 3 — Logical Compilation (`pipeline.py:21` `compile_logical_plan`)

Pure mapping, `SketchPlan → LogicalPlan`. Per relationship:
- `inheritance`, target is interface → `source.implements.append(target)`; target is not interface → `source.inherits_from = target`.
- `implements` → `source.implements.append(target)`.
- `composition` → `source.composes_with.append(target)`.
- `aggregation` → `source.aggregates_with.append(target)` (separate list — added this session; previously both composition and aggregation collapsed into `composes_with`, indistinguishable downstream).
- `association` → `source.associated_with.append(target)`.

`SemanticEntity` schema (`schemas/logical_plan.py:5`) carries `composes_with`, `aggregates_with`, `associated_with` as three independent optional lists.

---

## PHASE 4 — AST Bootstrap (`java_builder.py:9` `build_ast`)

Structure-only, no primitives. Per entity: `composes_with` → `JavaField(name=f"{target.lower()}s", type=target, is_collection=True)`. `aggregates_with` → identical shape (composition and aggregation are **structurally indistinguishable in Java code** — the difference is ownership-strength, a diagram/documentation-level concept only, not a code-level one). `associated_with` → `JavaField(name=target.lower(), type=target, is_collection=False)`.

---

## PHASE 5 — AST Enrichment (LLM) + Content Repair

### 5.1 Detail Generation
`gemini.py:208` `enrich_ast_with_details()`. Given the structural AST as JSON, invents primitive fields + business methods with short stub bodies. Four prompt constraints added this session:
- Always prefix field access with `this.` (reduces, does not eliminate, the case 5.8 must catch).
- Never invent getter/setter methods for existing fields (reduces 5.5 trigger frequency).
- If a class has a non-empty `implements` list, its methods must include one matching the interface's signature (reduces 5.9/Tier-B trigger frequency).

### Content Repair — Taxonomy A (deterministic, no LLM, `content_repair_pipeline.py`)

Executed via `ContentRepairPipeline.repair(ast_classes)`. Per class, then two whole-graph passes.

| # | Rule | Mechanism | Line |
|---|---|---|---|
| 5.2 | Reserved Keyword Sanitization | Field/method/parameter name in `JAVA_RESERVED_KEYWORDS` (49-entry set) → renamed `name + "_"` | 47–52 |
| 5.3 | Field Dedupe (own class) | Case-insensitive, first occurrence wins | 79–87 |
| 5.4 | Field Dedupe (inherited shadow) | Own field whose lowercase name matches any field in `get_inherited_fields()` (recursive `extends` walk) → dropped | 89–97 |
| 5.5 | Accessor Collision Removal | Auto-accessor signature set built from **own fields + inherited fields** (extended this session — originally own-fields-only, missed the case where a subclass's LLM-invented method collides with an ancestor's synthesized getter/setter); any method matching `(get<Field>, 0 params)` or `(set<Field>, 1 param)` → dropped | 99–116 |
| 5.6 | Method Self-Dedupe | `(name, tuple(param types))` signature set, own class only; later duplicate dropped | 118–128 |
| 5.7 | Invalid Override Rename | For each method, walk `_get_inherited_methods()` (real `JavaMethod` objects only — **does not** see 5.5's synthetic accessors, a known scope boundary); same `(name, params)` signature but different return type than the ancestor's real method → renamed `name + "Impl"` | 130–142 |
| 5.8 | Inherited-Field Visibility Promotion | Two-pass detection per method body string: (a) explicit `this.<field>` via regex, always trusted; (b) bare `<field>` via regex, trusted **unless** the field name collides with one of that method's own parameter names (setter-shadowing guard). Either match on a `private` inherited field → promote that field's `modifier` to `protected` on its *declaring* class | 144–163 |

**Confirmed bugs closed by 5.5/5.7/5.8** (all reproduced with real `javac`, not source-text guessing):
- 5.5: `getBalance()` duplicate-definition — LLM independently invented an accessor colliding with the auto-generated one, across 4 real classes in one live run (`Account`, `Customer`, `Transaction`, `BankAccount`).
- 5.7: subclass `getBalance()` with mismatched return type (`String` vs ancestor's synthetic `double`) — "cannot override... return type not compatible".
- 5.8: subclass method body doing `this.balance += 10` where `balance` is a private field on the superclass — "has private access in". Iteration 1 of this fix only matched `this.`-prefixed access (source-text regex); a second pass added bare-identifier matching after proving the `this.`-only version misses `balance += 10;` (also valid Java) with a live `javac` failure.

### Content Repair — Taxonomy B (LLM, `content_repair_pipeline.py` + `gemini.py:288`)

| # | Rule | Mechanism |
|---|---|---|
| 5.9 | Interface/Abstract Contract Fulfillment | `find_missing_contract_methods()` (line 174): for every non-abstract, non-interface class, walk its `extends` chain, collect (a) every method declared by any `implements`-ed interface anywhere in the chain, (b) every `is_abstract=True` method declared by an abstract ancestor in the chain; a signature is "fulfilled" if any class in the chain has a same-signature method with `body is not None`. Unfulfilled signatures → `gemini.py:288 fill_missing_contract_methods()` (batched, one call for the whole graph, not one per method) → LLM returns real bodies. Any signature the LLM didn't fill → `default_body_for_return_type()` deterministic stub (`return 0;` / `false;` / `'\0'` / `null;` / none for void) |

**Confirmed bug closed**: `SavingsAccount implements InterestBearing` shipped without `calculateInterest()` — "is not abstract and does not override abstract method" — live in `output/java_detailed` before this taxonomy existed.

---

## PHASE 6 — Compile Verification Gate (`compile_gate.py`, new this session)

Guarded by `is_javac_available()` (`shutil.which("javac")`) — silently skips (logs once) if no JDK on `PATH`, does not fail the pipeline.

### 6.1 Tier 1 — Deterministic Compiler-Error Repair (free, no LLM)
Loop, max 3 rounds: render `ast_classes` to a scratch temp dir, run real `javac`, on failure `parse_errors(stderr)` splits output into per-error blocks (`_split_error_blocks`, regex `^.+\.java:\d+: error:`) and classifies each against 5 known message shapes:

| Kind | javac message shape | Fix applied |
|---|---|---|
| `private_access` | `X has private access in Y` | Promote field `X` on class `Y` to `protected` |
| `duplicate_method` | `method X(...) is already defined in class Y` | Drop all but first occurrence of `X` on `Y` |
| `invalid_override` | `X(...) in Child cannot override X(...) in Parent` | Rename child's `X` → `XImpl` |
| `missing_override` | `Child is not abstract and does not override abstract method X(...) in Parent` | Copy `Parent.X`'s signature into `Child` with a deterministic stub body |
| `missing_symbol_class` | `cannot find symbol\n...symbol: class X` | If `X` is in `COMMON_TYPE_IMPORTS` (12-entry superset of `java_builder`'s own whitelist — `UUID`, `Optional`, `Instant`, `HashMap`, etc.), register the import permanently on the shared `JavaBuilder.standard_types` instance dict |

Any error not matching one of these 5 shapes is tagged `unknown` but still carries a `source_class` (extracted from the file-name header of its block) so it can still be routed to Tier 2.

Fix application is deduplicated per round (`(kind, owner_or_child, symbol_or_method)` key) to avoid redundant repeated log lines for the same underlying cause reported multiple times (e.g. a missing type referenced in constructor + getter + setter).

Loop exits early if a round produces zero fixes (nothing left Tier 1 can do).

### 6.2 Tier 2 — LLM Last-Resort Repair (costly, capped)
Triggers only if Tier 1 exhausted its 3 rounds without reaching a clean compile. **Hard cap: 1 attempt** (`max_tier2=1`, set by the caller in `detail_pipeline.py:94`). Collects `affected_classes` from the remaining errors' `owner`/`child`/`source_class` fields, sends their full current field/method state + the literal `javac` stderr to `gemini.py:245 fix_compile_errors()` (own small `tenacity` budget: 2 attempts, 3–15s backoff — for transient network failures only, not fix-quality retries). Response reused `DetailsResponse` schema (`{name, fields, methods}` per class) — full replacement of that class's fields/methods, not a delta.

### 6.3 Graceful Degradation
If Tier 2's single attempt still doesn't compile: log the remaining `javac` stderr verbatim, return the best-effort `ast_classes` anyway. Never raises, never loops beyond the caps, never silently claims success.

**Confirmed live behavior** (not just unit-tested):
- Tier 1 alone resolved a real `private_access` + `missing_symbol_class` combo (case: `UUID` field + bare-identifier `balance += 10`) with zero LLM calls.
- Tier 2 was observed live to *fail* on `animal.yaml`: root cause was actually a Phase 7.1 bug (missing `List` import from an inherited collection field — see below), and Tier 2's LLM response made it worse by hallucinating a spurious method literally named the same as the class. System did not crash; logged the residual error and shipped. Root cause was then fixed at 7.1, not patched in Phase 6 — Phase 6 is a safety net, not where permanent fixes belong.

---

## PHASE 7 — Rendering

### 7.1 Java Class Rendering (`java_builder.py:90` `render_class`)
- Import resolution: `self.standard_types` (10-entry instance dict, mutable — Tier 1's `missing_symbol_class` fix writes into this same instance) plus collection detection.
- **Fixed this session**: collection detection (`needs_list`) originally scanned only `java_class.fields` — missed collection fields that exist solely on an *ancestor* but still appear in this class's auto-generated constructor signature via `super(...)`. Now scans `list(java_class.fields) + get_inherited_fields(...)`. Confirmed live: `Mammal.java` constructor took `List<BodyPart>` with no `import java.util.List;` at all — `javac` "cannot find symbol: class List" — fixed at the source, not patched via Phase 6.
- Constructor: synthesized from `get_inherited_fields()` (super-call args) + own fields; `protected` if class `is_abstract`, else `public`.
- Getters/setters: synthesized for every `private`/`protected` field.
- Interface/abstract: `is_interface` suppresses the fields/constructor block entirely (interfaces hold no state — enforced upstream by 2.13, enforced again here structurally).

### 7.2 Class Diagram Rendering (`mermaid_builder.py`)
`build_class_diagram` (sketch-level, from `LogicalPlan`): `composes_with` → `*--` (filled diamond), `aggregates_with` → `o--` (hollow diamond, added this session — previously aggregation silently rendered as composition, the wrong UML symbol), `associated_with` → `-->`.
`build_detailed_diagram` (AST-level, from `JavaClass[]`): field/method access-modifier mapping, `extends`/`implements` arrows, collection-typed fields inferred as `*--` (this path still cannot distinguish aggregation from composition — see Known Limitations).

### 7.3 Assignment Document Rendering (`markdown_builder.py:11` `build_assignment`)
Constraint claims derived from **actually delivered content**, not from `preset.oop.*.enabled` toggles (a toggle being on does not guarantee 1.3 succeeded). `has_inheritance`/`has_abstract`/`has_interface` computed from `ast_classes` directly. `has_composition`/`has_aggregation` accept explicit caller-supplied booleans (computed by `detail_pipeline.py` from the Phase-3 `LogicalPlan`'s `composes_with`/`aggregates_with`, the only place downstream of Phase 4 where that distinction still exists) — falls back to an AST-only heuristic (cannot distinguish aggregation from composition, documented limitation, only used when no `LogicalPlan` is available to the caller) if not provided.

**Confirmed bugs closed**:
- Crash: `preset.oop.inheritance.enabled` accessed unguarded when `beginner.yaml` omits the `inheritance` block entirely (`AttributeError` on `None`) — the only unguarded access in the whole codebase, everywhere else already used the `if preset.oop and preset.oop.X` pattern.
- Silent content loss: Phase 3's `design_decisions` (a full LLM call) was generated, saved to `phase3_logical_plan.json`, and never read again — now loaded and rendered under `## 3. Design Decisions`.
- False claim: "Must include at least one interface" printed even when 1.3 failed to produce one (pre-1.3-existing) or when `ast_classes` genuinely had zero interfaces (`e_commerce.yaml`, live, before the Phase 1 hard-rule + domain hint fix).
- False claim: "Must use composition" printed for an entity whose only relationship was `aggregation` (both compile to identical `JavaField(is_collection=True)` shapes by Phase 4) — fixed by 7.3's `LogicalPlan`-sourced parameters.

---

## Test Coverage by Phase

| Phase | File | Test count (approx.) |
|---|---|---|
| 1 (1.2/1.3 retry logic) | `test_pipeline_guarantees.py` | 9 |
| 2 | `test_repair_pipeline.py` | ~35 (unit + property-based via Hypothesis) |
| 3–4 | `test_builders.py` | 8 |
| 5 (Taxonomy A + B) | `test_content_repair_pipeline.py` | 17 |
| 6 | not yet unit-tested as a file — verified live (3 real runs) + manual mock reproductions in-session, not committed as `pytest` cases |
| 7.1 | `test_constructor.py`, `test_builders.py` | 3 |
| 7.3 | `test_markdown_builder.py` | 9 |
| cross-cutting | `test_multi_implements.py` | 1 |

**Total: 67 passing** (`pytest tests/ -q`).

**Gap disclosed, not closed**: Phase 6 (`compile_gate.py`) has no committed `pytest` file — it was verified via live runs and ad-hoc reproduction scripts during this session but those weren't promoted to `tests/`. Same risk class as the other "0 test" gaps closed this session (silent regression potential), lower urgency because Phase 6 sits behind Phase 2/5's own tests and only activates on residual failures.

---

## Known Open Items (disclosed, not silently deferred)

1. **Generic unresolved-type resolution** (originally scoped as "rule 4.7", deliberately never built as a standalone rule). Only a fixed 12+10-entry whitelist (7.1 + Tier 1's `COMMON_TYPE_IMPORTS`) resolves missing imports deterministically; anything outside it falls through to Phase 6 Tier 2 (capped, not guaranteed).
2. **`build_detailed_diagram`** (7.2, AST-level) still cannot distinguish aggregation from composition — only the sketch-level diagram and 7.3's markdown were fixed, because only those two read from the `LogicalPlan` directly; the AST-level diagram reads from `JavaClass[]`, which lost the distinction at Phase 4.
3. **Cost/latency**: up to 7–8 LLM calls per generation now (1.1, 1.2×≤3, 1.3×≤2, design-decisions, 5.1, 5.9, 6.2×≤1) versus 1 in the original single-shot design. Not measured at scale.
4. **Domain/preset combinatorial coverage**: 5 domains × 3 presets = 15 combinations exist; approximately 8 distinct combinations were live-verified this session (`javac` exit 0 confirmed on each). Not exhaustive by design decision (diminishing returns disclosed and accepted mid-session, not an oversight).
5. **Semantic/domain-quality correctness** (is a relationship *the right* relationship, is stub logic *meaningful*) remains outside any automated check in this system — orthogonal to everything in Phases 2–7, which only guarantee structural validity and compile-safety, never domain correctness. This is a design boundary, not a bug.
6. Windows-specific transient file-lock on `shutil.rmtree(output_dir)` observed once during live testing (`PermissionError: [WinError 32]`), self-resolved on retry, not hardened against (no retry/backoff wrapper added).
