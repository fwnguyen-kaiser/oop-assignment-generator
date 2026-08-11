import pytest
from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship
from src.schemas.blueprint import BlueprintPreset, StructureConfig, ClassCountConfig, OopConfig, FeatureToggle, InheritanceConfig
from src.schemas.domain import DomainConfig
from src.validator.repair_pipeline import StructuralRepairPipeline

def make_sketch(entities, relationships):
    """Helper build SketchPlan thủ công, không cần LLM"""
    return SketchPlan(
        design_rationale="test",
        entities=[SketchEntity(name=n, kind="core", note="") for n in entities],
        relationships=[SketchRelationship(from_entity=f, to_entity=t, type=ty)
                        for f, t, ty in relationships]
    )

def make_pipeline(aggregation_enabled=True):
    preset = BlueprintPreset(
        difficulty="test",
        structure=StructureConfig(classes=ClassCountConfig(min=1, max=10)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=3),
            aggregation=FeatureToggle(enabled=aggregation_enabled)
        )
    )
    domain = DomainConfig(name="test", description="test", keywords=[], style="test")
    return StructuralRepairPipeline(preset, domain)

class TestNormalization:
    def test_self_loop_dropped(self):
        sketch = make_sketch(["A"], [("A", "A", "composition")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.relationships) == 0

    def test_normal_edge_not_dropped(self):
        sketch = make_sketch(["A", "B"], [("A", "B", "composition")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.relationships) == 1
        
    def test_dangling_edge_pruned(self):
        sketch = make_sketch(["A"], [("A", "Ghost", "association")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.relationships) == 0
        
    def test_case_insensitive_dedupe(self):
        sketch = make_sketch(["BankAccount", "Bankaccount"], [])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.entities) == 1
        assert result.entities[0].name.lower() == "bankaccount"
        
    def test_dedupe_remaps_relationship_references(self):
        sketch = make_sketch(
            ["BankAccount", "Bankaccount", "Customer"],
            [("Customer", "Bankaccount", "association")]  
        )
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.relationships) == 1
        assert result.relationships[0].to_entity == "BankAccount"

class TestMultipleInheritance:
    def test_second_parent_downgraded(self):
        sketch = make_sketch(["A", "B", "C"],
            [("A", "B", "inheritance"), ("A", "C", "inheritance")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        types = {(r.from_entity, r.to_entity): r.type for r in result.relationships}
        assert types[("A", "B")] == "inheritance"
        assert types[("A", "C")] == "association"

class TestCycleDetection:
    def test_two_node_cycle_broken(self):
        sketch = make_sketch(["A", "B"],
            [("A", "B", "inheritance"), ("B", "A", "inheritance")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        inh_edges = [r for r in result.relationships if r.type == "inheritance"]
        assert len(inh_edges) == 1

    def test_three_node_cycle_broken(self):
        sketch = make_sketch(["A", "B", "C"],
            [("A", "B", "inheritance"), ("B", "C", "inheritance"), ("C", "A", "inheritance")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        inh_edges = [r for r in result.relationships if r.type == "inheritance"]
        assert len(inh_edges) == 2

class TestTransitiveConflict:
    def test_grandparent_composition_conflict(self):
        sketch = make_sketch(["A", "B", "C"],
            [("A", "B", "inheritance"),
             ("B", "C", "inheritance"),
             ("A", "C", "composition")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        comp_edges = [r for r in result.relationships if r.type == "composition"]
        assert len(comp_edges) == 0

class TestCompositionCycles:
    def test_composition_cycle_downgraded(self):
        sketch = make_sketch(["A", "B"],
            [("A", "B", "composition"), ("B", "A", "composition")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        comp_edges = [r for r in result.relationships if r.type == "composition"]
        assert len(comp_edges) == 1

class TestMutationSafety:
    def test_input_sketch_not_mutated(self):
        original = make_sketch(["A", "B"], [("A", "B", "aggregation")])
        original_type_before = original.relationships[0].type
        pipeline = make_pipeline(aggregation_enabled=False)
        pipeline.repair(original)
        assert original.relationships[0].type == original_type_before

class TestDepthConstraint:
    def test_depth_exceeded_reattached(self):
        # A -> B -> C -> D
        # Max depth is 3. Root is A (depth 0). B(1), C(2), D(3).
        # Wait, if max_depth is 2, D should be reattached.
        sketch = make_sketch(["A", "B", "C", "D"], [
            ("B", "A", "inheritance"),
            ("C", "B", "inheritance"),
            ("D", "C", "inheritance")
        ])
        pipeline = make_pipeline()
        pipeline.preset.oop.inheritance.max_depth = 2
        result = pipeline.repair(sketch)
        # B->A, C->B (valid)
        # D->C is depth 3. Reattached to ancestor at depth 1 (B)
        # So D->B
        inherits = {r.from_entity: r.to_entity for r in result.relationships if r.type == "inheritance"}
        assert inherits["D"] == "B"

class TestExcessClasses:
    def test_excess_class_dropped(self):
        sketch = make_sketch(["A", "B", "C", "D", "E"], [])
        pipeline = make_pipeline()
        pipeline.preset.structure.classes.max = 4
        result = pipeline.repair(sketch)
        assert len(result.entities) == 4

class TestIsolatedComponents:
    def test_isolated_core_associated(self):
        sketch = make_sketch(["A", "B"], [])
        # Both are core. B should be associated to A.
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.relationships) == 1
        assert result.relationships[0].type == "association"

    def test_isolated_non_core_dropped(self):
        sketch = SketchPlan(
            design_rationale="",
            entities=[
                SketchEntity(name="A", kind="core", note=""),
                SketchEntity(name="B", kind="core", note=""),
                SketchEntity(name="C", kind="supporting", note="")
            ],
            relationships=[
                SketchRelationship(from_entity="A", to_entity="B", type="association")
            ]
        )
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        assert len(result.entities) == 2
        assert "C" not in [e.name for e in result.entities]

class TestInterfaceDeduction:
    def test_interface_deduced(self):
        # We rename A to "Payable" and give it kind="supporting" to pass the semantic gate
        entities = [
            SketchEntity(name="Payable", kind="supporting", note="Can be paid"),
            SketchEntity(name="B", kind="core", note=""),
            SketchEntity(name="C", kind="core", note="")
        ]
        relationships = [
            SketchRelationship(from_entity="B", to_entity="Payable", type="inheritance"),
            SketchRelationship(from_entity="C", to_entity="Payable", type="inheritance")
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)
        
        pipeline = make_pipeline()
        from src.schemas.blueprint import FeatureToggle
        pipeline.preset.oop.interface = FeatureToggle(enabled=True)
        result = pipeline.repair(sketch)
        a_entity = next(e for e in result.entities if e.name == "Payable")
        assert a_entity.is_interface == True

class TestOrphanInterface:
    def test_orphan_interface_set_by_llm_is_demoted(self):
        """Reproduces the real E-Commerce run: PaymentProcessor was marked is_interface=True
        directly by the LLM in Phase 1, but only ever referenced via an 'association' edge
        (Order -> PaymentProcessor), never 'implements' - so nothing actually implements it."""
        entities = [
            SketchEntity(name="Order", kind="core", note=""),
            SketchEntity(name="PaymentProcessor", kind="supporting", note="Interface defining transaction operations", is_interface=True),
        ]
        relationships = [
            SketchRelationship(from_entity="Order", to_entity="PaymentProcessor", type="association"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)

        pipeline = make_pipeline()
        from src.schemas.blueprint import FeatureToggle
        pipeline.preset.oop.interface = FeatureToggle(enabled=True)
        result = pipeline.repair(sketch)

        pp = next(e for e in result.entities if e.name == "PaymentProcessor")
        assert pp.is_interface is False

    def test_interface_with_implementer_not_demoted(self):
        entities = [
            SketchEntity(name="CreditCardProcessor", kind="core", note=""),
            SketchEntity(name="PaymentProcessor", kind="supporting", note="Interface", is_interface=True),
        ]
        relationships = [
            SketchRelationship(from_entity="CreditCardProcessor", to_entity="PaymentProcessor", type="implements"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)

        pipeline = make_pipeline()
        from src.schemas.blueprint import FeatureToggle
        pipeline.preset.oop.interface = FeatureToggle(enabled=True)
        result = pipeline.repair(sketch)

        pp = next(e for e in result.entities if e.name == "PaymentProcessor")
        assert pp.is_interface is True

    def test_orphan_interface_never_ships_even_when_excess_class_pressure_exists(self):
        """2.5 (excess classes, runs inside the convergence loop) and 2.11 (orphan interface
        demotion, runs once after the loop) can both apply to the same entity depending on
        timing - what must always hold, regardless of which one 'gets there first', is that
        the final output never contains an orphan interface (one with no implementer/inheritor)."""
        entities = [
            SketchEntity(name="Product", kind="core", note=""),
            SketchEntity(name="Order", kind="core", note=""),
            SketchEntity(name="PaymentGateway", kind="supporting", note="Orphan interface, no implementer", is_interface=True),
        ]
        relationships = [
            SketchRelationship(from_entity="Order", to_entity="Product", type="composition"),
            SketchRelationship(from_entity="Order", to_entity="PaymentGateway", type="association"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)

        preset = BlueprintPreset(
            difficulty="test",
            structure=StructureConfig(classes=ClassCountConfig(min=1, max=2)),
            oop=OopConfig(
                inheritance=InheritanceConfig(enabled=True, max_depth=3),
                interface=FeatureToggle(enabled=True),
            ),
        )
        domain = DomainConfig(name="test", description="test", keywords=[], style="test")
        pipeline = StructuralRepairPipeline(preset, domain)
        result = pipeline.repair(sketch)

        assert len(result.entities) <= 2
        implements_targets = {r.to_entity for r in result.relationships if r.type == "implements"}
        inheritance_targets = {r.to_entity for r in result.relationships if r.type == "inheritance"}
        for e in result.entities:
            if e.is_interface:
                assert e.name in implements_targets or e.name in inheritance_targets, (
                    f"{e.name} shipped as an orphan interface with no implementer/inheritor"
                )

    def test_orphan_interface_demoted_when_room_under_max(self):
        """With enough headroom under max_classes, 2.5 never triggers, isolating 2.11's own
        behavior: the orphan interface must be demoted, not silently left as-is."""
        entities = [
            SketchEntity(name="Product", kind="core", note=""),
            SketchEntity(name="Order", kind="core", note=""),
            SketchEntity(name="PaymentGateway", kind="supporting", note="Orphan interface, no implementer", is_interface=True),
        ]
        relationships = [
            SketchRelationship(from_entity="Order", to_entity="Product", type="composition"),
            SketchRelationship(from_entity="Order", to_entity="PaymentGateway", type="association"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)

        preset = BlueprintPreset(
            difficulty="test",
            structure=StructureConfig(classes=ClassCountConfig(min=1, max=10)),
            oop=OopConfig(
                inheritance=InheritanceConfig(enabled=True, max_depth=3),
                interface=FeatureToggle(enabled=True),
            ),
        )
        domain = DomainConfig(name="test", description="test", keywords=[], style="test")
        pipeline = StructuralRepairPipeline(preset, domain)
        result = pipeline.repair(sketch)

        assert len(result.entities) == 3
        pg = next(e for e in result.entities if e.name == "PaymentGateway")
        assert pg.is_interface is False
        steps = [log["step"] for log in pipeline.action_log]
        assert "2.11_orphan_interface" in steps

    def test_interface_with_inheritor_not_demoted(self):
        entities = [
            SketchEntity(name="B", kind="core", note=""),
            SketchEntity(name="Payable", kind="supporting", note="", is_interface=True),
        ]
        relationships = [
            SketchRelationship(from_entity="B", to_entity="Payable", type="inheritance"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)

        pipeline = make_pipeline()
        result = pipeline.repair(sketch)

        payable = next(e for e in result.entities if e.name == "Payable")
        assert payable.is_interface is True


# --- Tầng 2: Property-Based Tests ---
from hypothesis import given, strategies as st
import string

# Generates valid PascalCase names
entity_name_strategy = st.from_regex(r'^[A-Z][a-zA-Z0-9]{0,10}$', fullmatch=True)

@st.composite
def sketch_strategy(draw):
    names = draw(st.lists(entity_name_strategy, min_size=2, max_size=8, unique=True))
    
    # Generate random edges between these names
    edge_types = ["inheritance", "composition", "aggregation", "association"]
    edges = draw(st.lists(
        st.tuples(
            st.sampled_from(names),
            st.sampled_from(names),
            st.sampled_from(edge_types)
        ),
        min_size=0, max_size=15
    ))
    
    return make_sketch(names, edges)

class TestProperties:
    @given(sketch_strategy())
    def test_repaired_output_never_has_inheritance_cycle(self, sketch):
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        
        # Build graph to check for cycles
        adj = {e.name: [] for e in result.entities}
        for r in result.relationships:
            if r.type == "inheritance":
                # Ensure child and parent still exist
                if r.from_entity in adj and r.to_entity in adj:
                    adj[r.from_entity].append(r.to_entity)
                
        visited = set()
        rec_stack = set()
        
        def has_cycle(node):
            visited.add(node)
            rec_stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.remove(node)
            return False
            
        for node in adj:
            if node not in visited:
                assert not has_cycle(node), f"Cycle detected starting at {node}"

    @given(sketch_strategy())
    def test_repaired_output_respects_single_parent(self, sketch):
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        
        from collections import Counter
        child_count = Counter(r.from_entity for r in result.relationships if r.type == "inheritance")
        assert all(c <= 1 for c in child_count.values())

    @given(sketch_strategy())
    def test_no_entity_lost_or_duplicated(self, sketch):
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        
        original_names = {e.name.lower() for e in sketch.entities}
        result_names = {e.name.lower() for e in result.entities}
        
        # We don't add entities in this phase, so result MUST be exactly equal 
        # to the deduplicated original names.
        assert result_names == original_names
