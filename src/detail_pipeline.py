import json
import os
import yaml
from src.llm.gemini import GeminiProvider
from src.schemas.domain import DomainConfig
from src.schemas.blueprint import BlueprintPreset
from src.schemas.java_ast import JavaClass, JavaMethod
from src.builders.java_builder import JavaBuilder

def run_detail_pipeline(domain_path: str = "configs/domains/rpg_game.yaml", preset_path: str = "configs/presets/intermediate.yaml"):
    # 1. Load domain config for prompt context
    with open(domain_path, "r", encoding="utf-8") as f:
        domain = DomainConfig(**yaml.safe_load(f))
        
    # 2. Load the structural AST
    ast_file = "output/phase_e_java_ast.json"
    if not os.path.exists(ast_file):
        print(f"Error: {ast_file} not found. Run pipeline.py first.")
        return
        
    with open(ast_file, "r", encoding="utf-8") as f:
        ast_json_str = f.read()
        ast_data = json.loads(ast_json_str)
        ast_classes = [JavaClass(**c) for c in ast_data]
        
    print(f"Loaded {len(ast_classes)} classes from structural AST.")
    
    # 3. Call LLM to invent details
    print("Calling LLM to generate internal attributes and methods (Phase 4)...")
    from dotenv import load_dotenv
    load_dotenv()
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in environment or .env file")
        
    provider = GeminiProvider(api_key=api_key)

    # 3a. Phase 5a-i: LLM invents field/method SIGNATURES only (no bodies yet - the
    # response schema structurally has no body field, see GeminiProvider.generate_signatures).
    signature_entities = provider.generate_signatures(ast_json_str, domain.name, domain.description)
    print(f"LLM generated signatures for {len(signature_entities)} classes.")

    signature_map = {e.name: e for e in signature_entities}
    for cls in ast_classes:
        if cls.name in signature_map:
            sig = signature_map[cls.name]
            # Append LLM fields (primitive) to existing fields (relationships)
            cls.fields.extend(sig.fields)
            # Append LLM method signatures, body=None until Phase 5a-ii below
            cls.methods.extend(
                JavaMethod(
                    modifier=m.modifier, return_type=m.return_type, name=m.name,
                    parameters=m.parameters, body=None, is_abstract=m.is_abstract,
                )
                for m in sig.methods
            )

    from src.validator.content_repair_pipeline import ContentRepairPipeline, default_body_for_return_type, method_signature
    content_repair = ContentRepairPipeline()

    # 3b. Phase 5a-ii (structural pass): dedupe/rename/drop colliding signatures BEFORE
    # spending an LLM call on bodies for methods that would just get pruned. All bodies
    # are still None here, so rule 4.8 (protected-promotion, which needs real body text
    # to know what a method actually accesses) is a no-op this pass - it fires on the
    # second call below, after bodies exist.
    ast_classes = content_repair.repair(ast_classes)
    for log in content_repair.action_log:
        print(f"[CONTENT_REPAIR] {log['step']} on {log['node']}: {log['detail']} -> {log['action']}")
    _log_count_after_3b = len(content_repair.action_log)

    # 3c. Phase 5a-ii (body pass): now that the signature set is final, ask the LLM for
    # bodies. Interface methods and anything explicitly left abstract never get one
    # (matches how JavaBuilder renders them - see build_and_save/render_class).
    pending_bodies: dict[str, list[JavaMethod]] = {}
    for cls in ast_classes:
        if cls.is_interface:
            continue
        for m in cls.methods:
            if m.body is None and not m.is_abstract:
                pending_bodies.setdefault(cls.name, []).append(m)

    if pending_bodies:
        total_pending = sum(len(v) for v in pending_bodies.values())
        print(f"Requesting bodies for {total_pending} method(s) across {len(pending_bodies)} class(es)...")
        try:
            fills = provider.fill_signature_bodies(pending_bodies, ast_classes, domain.name, domain.description)
        except Exception as e:
            print(f"[WARNING] Failed to fetch method bodies via LLM: {e}")
            fills = []

        # Keyed by the full JLS method signature (name + parameter TYPES), not name alone -
        # (class_name, method_name) is not a valid Java method identity, two methods can
        # share a name with different parameters (overloading). See method_signature()'s
        # docstring for why this matters and the mini-grader precedent for this exact class
        # of bug.
        fill_map = {(f.class_name, f.method_name, tuple(f.param_types)): f.body for f in fills}
        for class_name, methods in pending_bodies.items():
            for m in methods:
                body = fill_map.get((class_name,) + method_signature(m))
                if body is None:
                    body = default_body_for_return_type(m.return_type)
                    print(f"[CONTENT_REPAIR] 5a-ii fallback stub used for {class_name}.{m.name}")
                m.body = body

    # 3d. Re-run the deterministic pass now that bodies exist, specifically for rule 4.8
    # (protected-promotion for direct inherited-field access) - re-running the earlier
    # rules too is harmless/idempotent, the signature set is already clean from 3b. Only
    # print entries logged in THIS call, since 3b's were already printed above.
    ast_classes = content_repair.repair(ast_classes)
    for log in content_repair.action_log[_log_count_after_3b:]:
        print(f"[CONTENT_REPAIR] {log['step']} on {log['node']}: {log['detail']} -> {log['action']}")

    # 4.5b Content Repair: interface/abstract contract fulfillment
    missing = content_repair.find_missing_contract_methods(ast_classes)
    if missing:
        total_missing = sum(len(v) for v in missing.values())
        print(f"Found {total_missing} unimplemented contract method(s) across {len(missing)} class(es). Requesting real implementations...")
        try:
            fills = provider.fill_missing_contract_methods(missing, ast_classes, domain.name, domain.description)
        except Exception as e:
            print(f"[WARNING] Failed to fetch contract method implementations via LLM: {e}")
            fills = []

        # See the identical fix above (5a-ii) for why this is keyed by full JLS method
        # signature, not (class_name, method_name) alone.
        fill_map = {(f.class_name, f.method_name, tuple(f.param_types)): f.body for f in fills}
        class_map = {c.name: c for c in ast_classes}
        for class_name, methods in missing.items():
            cls = class_map[class_name]
            for m in methods:
                body = fill_map.get((class_name,) + method_signature(m))
                if body is None:
                    body = default_body_for_return_type(m.return_type)
                    print(f"[CONTENT_REPAIR] 4.4/4.5 fallback stub used for {class_name}.{m.name}")
                new_method = m.model_copy(deep=True)
                new_method.body = body
                new_method.is_abstract = False
                cls.methods.append(new_method)

    # 4.6 Phase 6: Compile Verification Gate (ultimate safeguard, not primary fix mechanism)
    # Tier 1 (free, deterministic) reads the REAL javac error and auto-fixes known shapes.
    # Tier 2 (costly, capped to 1 attempt) is a last-resort LLM call for genuinely novel
    # errors that don't match any known pattern. Disclosed limitation: this is best-effort -
    # if both tiers are exhausted, the output ships anyway with a clearly logged warning
    # rather than silently failing or looping indefinitely.
    from src.validator.compile_gate import CompileVerificationGate
    java_builder = JavaBuilder()
    compile_gate = CompileVerificationGate(java_builder)
    ast_classes = compile_gate.verify_and_repair(ast_classes, domain, provider, max_tier1=3, max_tier2=1)

    # 5. Save detailed AST
    with open("output/phase4_detailed_ast.json", "w", encoding="utf-8") as f:
        json.dump([node.model_dump() for node in ast_classes], f, indent=2)
    print("Saved detailed AST to output/phase4_detailed_ast.json")
    
    # 6. Render the detailed Mermaid diagram
    from src.builders.mermaid_builder import MermaidBuilder
    mermaid_builder = MermaidBuilder()
    detailed_mmd = mermaid_builder.build_detailed_diagram(ast_classes)
    with open("output/class_diagram_detailed.mmd", "w", encoding="utf-8") as f:
        f.write(detailed_mmd)
    print("Saved detailed Mermaid diagram to output/class_diagram_detailed.mmd")
    
    # 7. Render the final, full Java files & Skeletons
    # Reuse the SAME JavaBuilder instance created for the compile gate above - it may have
    # learned new standard_types import mappings via Tier 1 that must carry over to this render.
    output_dir = "output/java_detailed"
    skeleton_dir = "output/java_skeleton"
    
    import shutil
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    if os.path.exists(skeleton_dir):
        shutil.rmtree(skeleton_dir)
        
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(skeleton_dir, exist_ok=True)
    import copy
    
    skeletons_dict = {}
    class_map = {c.name: c for c in ast_classes}
    
    for cls in ast_classes:
        code = java_builder.render_class(cls, class_map)
        # Write full solution
        with open(os.path.join(output_dir, f"{cls.name}.java"), "w", encoding="utf-8") as f:
            f.write(code)
            
        # Write skeleton using AST modification
        skeleton_cls = copy.deepcopy(cls)
        for m in skeleton_cls.methods:
            if not m.return_type:
                m.body = None
            else:
                ret_name = m.return_type.name
                if ret_name == "void":
                    m.body = None
                elif ret_name in ["int", "short", "long", "byte", "double", "float"]:
                    m.body = "return 0;"
                elif ret_name == "boolean":
                    m.body = "return false;"
                elif ret_name == "char":
                    m.body = "return '\\0';"
                else:
                    m.body = "return null;"
            
        skeleton_code = java_builder.render_class(skeleton_cls, class_map)
            
        with open(os.path.join(skeleton_dir, f"{cls.name}.java"), "w", encoding="utf-8") as f:
            f.write(skeleton_code)
        skeletons_dict[cls.name] = skeleton_code
            
    print(f"Rendered {len(ast_classes)} detailed Java files and skeletons to output/")
    
    # 8. Render Final Assignment Markdown
    from src.builders.markdown_builder import MarkdownBuilder
    with open(preset_path, "r", encoding="utf-8") as f:
        preset = BlueprintPreset(**yaml.safe_load(f))

    logical_plan_file = "output/phase3_logical_plan.json"
    design_decisions = []
    has_composition = None
    has_aggregation = None
    if os.path.exists(logical_plan_file):
        with open(logical_plan_file, "r", encoding="utf-8") as f:
            logical_plan_data = json.load(f)
        design_decisions = logical_plan_data.get("design_decisions", [])
        all_entities = logical_plan_data.get("domain_entities", []) + logical_plan_data.get("support_entities", [])
        has_composition = any(e.get("composes_with") for e in all_entities)
        has_aggregation = any(e.get("aggregates_with") for e in all_entities)

    md_builder = MarkdownBuilder()
    assignment_md = md_builder.build_assignment(domain, preset, ast_classes, detailed_mmd, skeletons_dict, design_decisions, has_composition, has_aggregation)
    
    with open("output/assignment.md", "w", encoding="utf-8") as f:
        f.write(assignment_md)
        
    print("Saved final assignment to output/assignment.md")

if __name__ == "__main__":
    run_detail_pipeline()
