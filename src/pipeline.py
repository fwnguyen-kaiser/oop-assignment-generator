import yaml
import os
import json
from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.schemas.logical_plan import LogicalPlan, SketchPlan
from src.llm.context_builder import ContextBuilder
from src.llm.gemini import GeminiProvider
from src.validator.repair_pipeline import StructuralRepairPipeline

def validate_sketch(sketch: SketchPlan) -> list[str]:
    entity_names = {e.name for e in sketch.entities}
    errors = []
    for r in sketch.relationships:
        if r.from_entity not in entity_names:
            errors.append(f"dangling from_entity: {r.from_entity}")
        if r.to_entity not in entity_names:
            errors.append(f"dangling to_entity: {r.to_entity}")
    # Inheritance now lives on the entity itself (extends/extends_interfaces), not in
    # `relationships` - see the Tầng 0 schema change - so it needs its own dangling-ref
    # check here rather than being covered by the loop above.
    for e in sketch.entities:
        if e.extends and e.extends not in entity_names:
            errors.append(f"dangling extends: {e.name} -> {e.extends}")
        for target in e.extends_interfaces:
            if target not in entity_names:
                errors.append(f"dangling extends_interfaces: {e.name} -> {target}")
    return errors

def compile_logical_plan(sketch: SketchPlan, decisions: list[str]) -> LogicalPlan:
    from src.schemas.logical_plan import SemanticEntity
    from collections import defaultdict
    
    entities_map = {}
    
    # 1. Initialize SemanticEntities. Inheritance is read straight off the entity -
    # repair_pipeline already resolved it into class_parent_map/iface_parent_map and
    # wrote the final, kind-consistent result back onto e.extends/e.extends_interfaces
    # (see StructuralRepairPipeline.repair's last step) - so there's no relationship-
    # type routing decision left to make here for inheritance at all.
    for e in sketch.entities:
        entities_map[e.name] = SemanticEntity(
            name=e.name,
            description=e.note,
            is_abstract=e.is_abstract,
            is_interface=e.is_interface,
            inherits_from=(e.extends if not e.is_interface else None),
            extends_interfaces=(list(e.extends_interfaces) if e.is_interface and e.extends_interfaces else None),
            implements=[],
            composes_with=[],
            aggregates_with=[],
            associated_with=[]
        )

    # 2. Map structural relationships (composition/aggregation/association/implements -
    # "inheritance" is no longer a possible SketchRelationship.type value at all)
    for r in sketch.relationships:
        source = entities_map.get(r.from_entity)
        target = entities_map.get(r.to_entity)
        if not source or not target:
            continue

        if r.type == "implements":
            source.implements.append(target.name)
        elif r.type == "composition":
            source.composes_with.append(target.name)
        elif r.type == "aggregation":
            source.aggregates_with.append(target.name)
        elif r.type == "association":
            source.associated_with.append(target.name)

    # Remove empty lists to keep JSON clean
    for se in entities_map.values():
        if not se.implements: se.implements = None
        if not se.extends_interfaces: se.extends_interfaces = None
        if not se.composes_with: se.composes_with = None
        if not se.aggregates_with: se.aggregates_with = None
        if not se.associated_with: se.associated_with = None

    # 3. Partition by kind
    domain_entities = []
    support_entities = []
    
    for e in sketch.entities:
        se = entities_map[e.name]
        if e.kind == "core":
            domain_entities.append(se)
        else:
            support_entities.append(se)
            
    return LogicalPlan(
        design_decisions=decisions,
        domain_entities=domain_entities,
        support_entities=support_entities
    )

def apply_min_classes_guarantee(repaired_sketch: SketchPlan, llm, repair_engine: StructuralRepairPipeline, sanitized_domain: DomainConfig, min_classes: int, max_attempts: int = 3) -> SketchPlan:
    """Phase 1b - pure retry logic, no file I/O, so it can be unit tested with a mocked
    llm/repair_engine without touching the real output/ directory."""
    if len(repaired_sketch.entities) >= min_classes:
        return repaired_sketch

    for attempt in range(max_attempts):
        missing_count = min_classes - len(repaired_sketch.entities)
        if missing_count <= 0:
            break

        print(f"\n[Phase 1b - Attempt {attempt+1}] Graph has {len(repaired_sketch.entities)} entities (min {min_classes}). Requesting {missing_count} NEW entities...")
        try:
            missing_sketch = llm.generate_missing_entities(repaired_sketch, sanitized_domain, missing_count)
            repaired_sketch.entities.extend(missing_sketch.entities)
            repaired_sketch.relationships.extend(missing_sketch.relationships)

            print(f"\n--- Running Phase 2: Structural Repair (Round {attempt + 2}) ---")
            repaired_sketch = repair_engine.repair(repaired_sketch)
        except Exception as e:
            print(f"[ERROR] Phase 1b Failed: {e}")

    if len(repaired_sketch.entities) < min_classes:
        print(f"[WARNING] Phase 1b failed to meet min_classes ({min_classes}) after {max_attempts} attempts.")

    return repaired_sketch


def apply_interface_guarantee(repaired_sketch: SketchPlan, llm, repair_engine: StructuralRepairPipeline, sanitized_domain: DomainConfig, interface_required: bool, max_attempts: int = 2) -> SketchPlan:
    """Phase 1c - pure retry logic, no file I/O. Guarantees a required interface exists
    WITHOUT forcing an existing entity (forcing strips its state/relationships - see 2.10 -
    and destroys its domain meaning; the interface must come from a genuine, LLM-proposed
    semantic decision)."""
    if not interface_required or any(e.is_interface for e in repaired_sketch.entities):
        return repaired_sketch

    for attempt in range(max_attempts):
        if any(e.is_interface for e in repaired_sketch.entities):
            break

        print(f"\n[Phase 1c - Attempt {attempt+1}] Preset requires an interface but none exists. Requesting a new interface entity...")
        try:
            interface_sketch = llm.generate_missing_interface(repaired_sketch, sanitized_domain)
            repaired_sketch.entities.extend(interface_sketch.entities)
            repaired_sketch.relationships.extend(interface_sketch.relationships)

            print(f"\n--- Running Phase 2: Structural Repair (Interface Round {attempt + 1}) ---")
            repaired_sketch = repair_engine.repair(repaired_sketch)
        except Exception as e:
            print(f"[ERROR] Phase 1c Failed: {e}")

    if not any(e.is_interface for e in repaired_sketch.entities):
        print(f"[WARNING] Phase 1c failed to establish a genuine interface after {max_attempts} attempts. Proceeding without one.")

    return repaired_sketch


def run_pipeline(domain_path: str = "configs/domains/rpg_game.yaml", preset_path: str = "configs/presets/intermediate.yaml"):
    print(f"--- Starting V2 Pipeline Phase 1 & 2 for Domain: {domain_path} ---")
    
    # 1. Load Configurations
    with open(domain_path, "r", encoding="utf-8") as f:
        domain = DomainConfig(**yaml.safe_load(f))
        
    with open(preset_path, "r", encoding="utf-8") as f:
        preset = BlueprintPreset(**yaml.safe_load(f))
        
    # 2. Setup Context & Pruning
    cb = ContextBuilder()
    sanitized_domain = cb.sanitize_domain(domain, preset.oop)
    rag_context = cb.build_context(preset.oop)
    
    # 3. Setup LLM & Validator
    from dotenv import load_dotenv
    import os
    load_dotenv()
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in environment or .env file")
        
    llm = GeminiProvider(api_key)
    repair_engine = StructuralRepairPipeline(preset, sanitized_domain)
    
    print(f"--- Starting V2 Pipeline Phase 1 & 2 for Domain: {sanitized_domain.name} ---")
    
    max_retries = 3
    sketch_plan = None
    previous_errors = None
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"\n[Attempt {attempt}] Generating Sketch...")
            candidate_sketch = llm.generate_sketch(preset, sanitized_domain, rag_context, previous_errors)
            
            errors = validate_sketch(candidate_sketch)
            if errors:
                print(f"[FAILED] Sketch validation failed: {errors}")
                previous_errors = errors
                continue
                
            print("[SUCCESS] Phase 1 Sketch Plan generated and validated successfully!")
            sketch_plan = candidate_sketch
            break
            
        except Exception as e:
            print(f"[ERROR] Sketch Generation Error: {e}")
            if attempt == max_retries:
                print("\n[FATAL] Pipeline FAILED after maximum retries.")
                return
                
    if not sketch_plan:
        print("\n[FATAL] Failed to generate a valid sketch.")
        return
        
    # Save phase 1 output BEFORE repair to ensure it is untainted
    os.makedirs("output", exist_ok=True)
    with open("output/phase1_sketch_plan.json", "w", encoding="utf-8") as f:
        f.write(sketch_plan.model_dump_json(indent=2))
        
    print("\n--- Running Phase 2: Structural Repair ---")
    repaired_sketch = repair_engine.repair(sketch_plan)
    
    min_classes = preset.structure.classes.min
    repaired_sketch = apply_min_classes_guarantee(repaired_sketch, llm, repair_engine, sanitized_domain, min_classes)

    interface_required = preset.oop.interface.enabled if preset.oop and preset.oop.interface else False
    repaired_sketch = apply_interface_guarantee(repaired_sketch, llm, repair_engine, sanitized_domain, interface_required)

    for log in repair_engine.action_log:
        print(f"[REPAIR] {log['step']} on {log['node']}: {log['detail']} -> {log['action']}")

    lossiness = repair_engine.lossiness_summary()
    print(f"[REPAIR] lossiness summary: {lossiness}")
    # Appended (not overwritten) across runs - this is the raw data the "ĐO" step needs
    # to decide whether Phase 2.fb is worth building at all: average invent_or_destroy/
    # drop_edge actions per generate, across N domains x presets, measured AFTER Tầng 0
    # (schema) + Tầng 1 (prompt) are in place. A single run tells you nothing on its own.
    lossiness_log_path = "output/phase2_lossiness_log.jsonl"
    with open(lossiness_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"domain": sanitized_domain.name, **lossiness}) + "\n")

    with open("output/phase2_repaired_sketch.json", "w", encoding="utf-8") as f:
        f.write(repaired_sketch.model_dump_json(indent=2))

    print("\n--- Running Phase 2.5: Skeleton Compile Gate ---")
    from src.validator.skeleton_gate import SkeletonGate
    gate_ok, gate_stderr = SkeletonGate().check(repaired_sketch)
    if gate_ok:
        print("[PHASE 2.5] Skeleton compiles cleanly - structural repair is javac-legal.")
    else:
        # No retry loop yet (that's Phase 2.fb, deferred until the action_log-lossiness
        # metric shows it's actually needed - see docs). For now this just makes a
        # structural-legality failure visible immediately after Phase 2, instead of
        # only surfacing once Phase 6 fails after Phase 5a-i/ii already spent LLM calls
        # writing fields/methods for a graph that could never have compiled.
        print(f"[PHASE 2.5] WARNING: skeleton failed to compile - structural repair produced an illegal graph:\n{gate_stderr}")

    print("\n--- Running Phase 3: Logical Plan Compilation ---")
    try:
        decisions = llm.generate_design_decisions(repaired_sketch, repair_engine.action_log)
    except Exception as e:
        print(f"[WARNING] Failed to generate design decisions via LLM: {e}")
        decisions = ["Generated by StructuralRepairPipeline without LLM reasoning."]
        
    final_plan = compile_logical_plan(repaired_sketch, decisions)
    
    with open("output/phase3_logical_plan.json", "w", encoding="utf-8") as f:
        f.write(final_plan.model_dump_json(indent=2, exclude_none=True))
        
    print("\nSaved to output/phase1_sketch_plan.json")
    print("Saved to output/phase2_repaired_sketch.json")
    print("Saved to output/phase3_logical_plan.json")
    
    # --- Phase 4: AST Bootstrap / Output Generation ---
    # Was inconsistently lettered "Phase E" - renumbered to close the gap this created in
    # the 1/1b/1c/2/3/[5a-i]/[5a-ii]/6 sequence once src/detail_pipeline.py's Phase 4
    # (enrich_ast_with_details) was split into Phase 5a-i/5a-ii.
    print("\n--- Running Phase 4: AST Bootstrap / Output Generation ---")
    from src.builders.mermaid_builder import MermaidBuilder
    from src.builders.java_builder import JavaBuilder
    
    # 1. Mermaid
    mermaid_builder = MermaidBuilder()
    mmd_code = mermaid_builder.build_class_diagram(final_plan)
    with open("output/class_diagram.mmd", "w", encoding="utf-8") as f:
        f.write(mmd_code)
    print("Saved to output/class_diagram.mmd")
    
    # 2. Java AST
    java_builder = JavaBuilder()
    java_builder.build_and_save(final_plan, "output/java")
    print("Saved Java files to output/java/")
    
    return final_plan

if __name__ == "__main__":
    run_pipeline()
