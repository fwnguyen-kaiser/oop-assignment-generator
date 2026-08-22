import pytest
from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship
from src.schemas.blueprint import BlueprintPreset, StructureConfig, ClassCountConfig, OopConfig, FeatureToggle, InheritanceConfig
from src.schemas.domain import DomainConfig
from src.validator.repair_pipeline import StructuralRepairPipeline

def make_sketch(entities, relationships):
    """Helper build SketchPlan thủ công, không cần LLM. `relationships` is a list of
    (from, to, type) tuples - "inheritance" is a convenience alias handled here, NOT a
    real SketchRelationship.type value anymore (inheritance moved to
    SketchEntity.extends/extends_interfaces - see the Tầng 0 schema change in
    src/schemas/logical_plan.py). All entities built by this helper are plain classes
    (is_interface=False), so an "inheritance" tuple sets `.extends` on the FROM
    entity."""
    entity_objs = {n: SketchEntity(name=n, kind="core", note="") for n in entities}
    rels = []
    for f, t, ty in relationships:
        if ty == "inheritance":
            entity_objs[f].extends = t
        else:
            rels.append(SketchRelationship(from_entity=f, to_entity=t, type=ty))
    return SketchPlan(design_rationale="test", entities=list(entity_objs.values()), relationships=rels)

def make_pipeline(aggregation_enabled=True, max_classes=10):
    preset = BlueprintPreset(
        difficulty="test",
        structure=StructureConfig(classes=ClassCountConfig(min=1, max=max_classes)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=3),
            aggregation=FeatureToggle(enabled=aggregation_enabled)
        )
    )
    domain = DomainConfig(name="test", description="test", keywords=[], style="test")
    return StructuralRepairPipeline(preset, domain)


class TestExcessClassesBatchDrop:
    """EDGE-1 regression: rule 2.5 (excess classes) used to drop only one node per
    convergence-loop iteration, and the loop is capped at 10 iterations. With more
    than 10 excess classes, this silently left the graph over max_classes with no
    error - just a print. Verified live: max=5, 20 input entities used to survive at
    10, not 5."""

    def test_drops_all_excess_in_one_pass_even_beyond_the_iteration_cap(self):
        entities = [f"E{i}" for i in range(20)]
        relationships = [(f"E{i}", f"E{i+1}", "association") for i in range(19)]
        sketch = make_sketch(entities, relationships)
        pipeline = make_pipeline(max_classes=5)
        result = pipeline.repair(sketch)
        assert len(result.entities) == 5
        assert pipeline.converged is True

    def test_converged_flag_true_when_no_excess(self):
        sketch = make_sketch(["A", "B"], [("A", "B", "association")])
        pipeline = make_pipeline(max_classes=10)
        pipeline.repair(sketch)
        assert pipeline.converged is True


class TestPostCleanupIsolation:
    """EDGE-2 regression: 2.10_interface_state (post-loop) can drop an entity's only
    remaining edge, orphaning it with nothing to re-check isolation afterward - 2.8's
    isolation check only runs INSIDE the convergence loop, before 2.10 exists.
    Verified live: an interface's has-a edge to a core entity with no other
    relationships left that entity fully disconnected in the repaired output."""

    def test_entity_orphaned_by_interface_state_cleanup_gets_reattached(self):
        sketch = SketchPlan(
            design_rationale="test",
            entities=[
                SketchEntity(name="Iface", kind="supporting", note="", is_interface=True),
                SketchEntity(name="A", kind="core", note=""),
                SketchEntity(name="B", kind="core", note=""),
                SketchEntity(name="Lonely", kind="core", note=""),
            ],
            relationships=[
                SketchRelationship(from_entity="A", to_entity="Iface", type="implements"),
                SketchRelationship(from_entity="B", to_entity="Iface", type="implements"),
                SketchRelationship(from_entity="Iface", to_entity="Lonely", type="association"),
            ],
        )
        pipeline = make_pipeline(max_classes=10)
        result = pipeline.repair(sketch)

        adj = set()
        for r in result.relationships:
            adj.add(r.from_entity)
            adj.add(r.to_entity)
        orphans = [e.name for e in result.entities if e.name not in adj]
        assert orphans == []
        assert any(e.name == "Lonely" for e in result.entities)

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
    """The old 'second parent downgraded to implements' scenario is no longer
    representable: SketchEntity.extends is a single Optional[str], so the LLM
    literally cannot express two superclasses for one class (rule 2.1_multiple_inheritance
    is gone - the schema itself makes it unreachable). What remains testable is that a
    node CAN legitimately be the parent for several different children."""

    def test_multiple_children_can_share_one_parent(self):
        sketch = make_sketch(["A", "B", "C"],
            [("B", "A", "inheritance"), ("C", "A", "inheritance")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        extends = {e.name: e.extends for e in result.entities}
        assert extends["B"] == "A"
        assert extends["C"] == "A"

class TestCycleDetection:
    def test_two_node_cycle_broken(self):
        sketch = make_sketch(["A", "B"],
            [("A", "B", "inheritance"), ("B", "A", "inheritance")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        extends_count = sum(1 for e in result.entities if e.extends)
        assert extends_count == 1

    def test_three_node_cycle_broken(self):
        sketch = make_sketch(["A", "B", "C"],
            [("A", "B", "inheritance"), ("B", "C", "inheritance"), ("C", "A", "inheritance")])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        extends_count = sum(1 for e in result.entities if e.extends)
        assert extends_count == 2

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
        d = next(e for e in result.entities if e.name == "D")
        assert d.extends == "B"

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

class TestLossinessSummary:
    """Measurement infrastructure for the '2.fb worth building?' decision (see the
    LOSSINESS classification in repair_pipeline.py) - pure/deterministic, no LLM
    involved, safe to unit test directly."""

    def test_all_action_log_steps_are_classified(self):
        """Every log_action() call site's step string must have a LOSSINESS entry -
        an unclassified step would silently fall into 'unknown' and never get counted
        toward the real categories, defeating the whole measurement."""
        import re
        from src.validator.repair_pipeline import LOSSINESS
        src = open("src/validator/repair_pipeline.py", encoding="utf-8").read()
        steps_in_code = set(re.findall(r'log_action\("([0-9a-z_.]+)"', src))
        unclassified = steps_in_code - set(LOSSINESS.keys())
        assert not unclassified, f"steps missing from LOSSINESS: {unclassified}"

    def test_normalize_actions_dont_count_as_severe(self):
        sketch = make_sketch(["BankAccount", "Bankaccount"], [])
        pipeline = make_pipeline()
        pipeline.repair(sketch)
        summary = pipeline.lossiness_summary()
        assert summary["normalize"] >= 1
        assert summary["invent_or_destroy"] == 0
        assert summary["unknown"] == 0

    def test_node_destruction_counts_as_invent_or_destroy(self):
        sketch = make_sketch(["A", "B", "C"], [])
        pipeline = make_pipeline(max_classes=2)
        pipeline.repair(sketch)
        summary = pipeline.lossiness_summary()
        assert summary["invent_or_destroy"] >= 1


class TestFinalEdgeInvariantSweep:
    """Found by an independent audit + follow-up fuzzing (80 then 300 adversarial
    trials, seeds 42 and 777, all run through repair -> compile_logical_plan ->
    JavaBuilder -> real javac): several rules append a new structural edge as part of
    downgrading a mismatch (2.1a, 2.1b, 2.10c), and each one skips the mid-pipeline
    2.10 filters that were supposed to catch exactly this shape - because those
    filters ran BEFORE the edge that violates them was even created. 2.14/2.15/2.16
    are the final, order-independent sweep that catches all of this regardless of
    which earlier rule created the bad edge."""

    def test_interface_that_only_becomes_interface_after_its_own_downgrade_never_ships_implements(self):
        """Live-found repro: E5 starts as a plain class with extends='E1'. Both E5 and
        E1 get derived as interfaces by 2.6 IN THE SAME repair() call (each received
        2+ implements-edges from other entities). 2.10c then downgrades E5's now-
        invalid class_parent_map entry into an 'implements E1' edge - but by this
        point E5 itself is ALREADY an interface, so that edge is illegal Java
        (interfaces can't implement anything) and used to survive because 2.10's own
        interface-state filter had already run before 2.10c existed."""
        entities = [
            SketchEntity(name="E1", kind="supporting", note="", is_interface=False),
            SketchEntity(name="E5", kind="supporting", note="", is_interface=False, extends="E1"),
            SketchEntity(name="X", kind="core", note="", extends=None),
            SketchEntity(name="Y", kind="core", note=""),
            SketchEntity(name="Z", kind="core", note=""),
        ]
        relationships = [
            SketchRelationship(from_entity="X", to_entity="E1", type="implements"),
            SketchRelationship(from_entity="Y", to_entity="E1", type="implements"),
            SketchRelationship(from_entity="Z", to_entity="E5", type="implements"),
            SketchRelationship(from_entity="X", to_entity="E5", type="implements"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)
        pipeline = make_pipeline()
        from src.schemas.blueprint import FeatureToggle
        pipeline.preset.oop.interface = FeatureToggle(enabled=True)
        result = pipeline.repair(sketch)

        by_name = {e.name: e for e in result.entities}
        assert by_name["E5"].is_interface is True
        # No surviving structural edge may have an interface as its source.
        for r in result.relationships:
            source = by_name.get(r.from_entity)
            assert not (source and source.is_interface), (
                f"{r.from_entity} (interface) still has an outgoing {r.type} edge to {r.to_entity}"
            )

    def test_has_a_edge_to_same_target_as_an_ancestor_is_dropped(self):
        """Live-found repro: a subclass independently declaring a has-a edge to the
        same target one of its ancestors already has produces `public E1(List<E0>
        e0s, List<E0> e0s)` - a real javac 'variable already defined' error, since
        JavaBuilder.get_inherited_fields merges ancestor fields with the subclass's
        own by name only."""
        entities = [
            SketchEntity(name="Parent", kind="core", note=""),
            SketchEntity(name="Child", kind="core", note="", extends="Parent"),
            SketchEntity(name="Shared", kind="core", note=""),
        ]
        relationships = [
            SketchRelationship(from_entity="Parent", to_entity="Shared", type="composition"),
            SketchRelationship(from_entity="Child", to_entity="Shared", type="composition"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)

        child_targets = {r.to_entity for r in result.relationships if r.from_entity == "Child" and r.type == "composition"}
        assert "Shared" not in child_targets
        parent_targets = {r.to_entity for r in result.relationships if r.from_entity == "Parent" and r.type == "composition"}
        assert "Shared" in parent_targets

    def test_own_composition_and_aggregation_to_same_target_collapse_to_one_edge(self):
        """Live-found repro: the SAME (from, to) pair declared as both composition and
        aggregation renders two fields with the identical name (`{target.lower()}s`
        either way) - `private List<E0> e0s;` twice."""
        entities = [
            SketchEntity(name="A", kind="core", note=""),
            SketchEntity(name="B", kind="core", note=""),
        ]
        relationships = [
            SketchRelationship(from_entity="A", to_entity="B", type="composition"),
            SketchRelationship(from_entity="A", to_entity="B", type="aggregation"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=relationships)
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)

        edges_a_to_b = [r for r in result.relationships if r.from_entity == "A" and r.to_entity == "B"]
        assert len(edges_a_to_b) == 1


class TestClassExtendsInterfaceNormalization:
    """GAP-2: a class/abstract class can never `extends` an interface in Java (only
    `implements` it). Verified live (real javac): a class with `extends` pointing at
    an interface produced a syntax error, and Phase 3 used to silently paper over it
    downstream - repair_pipeline itself now normalizes this immediately."""

    def test_explicit_class_extends_interface_downgraded_to_implements_immediately(self):
        """2.1a: the mismatch is already known at loop-start (Payable is already
        is_interface=True before repair begins), so this must be caught inside the
        convergence loop, not just by the post-loop 2.10c safety net."""
        entities = [
            SketchEntity(name="Payable", kind="supporting", note="", is_interface=True),
            SketchEntity(name="Invoice", kind="core", note="", extends="Payable"),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=[])
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)

        invoice = next(e for e in result.entities if e.name == "Invoice")
        assert invoice.extends is None
        impl_edges = [r for r in result.relationships if r.type == "implements" and r.from_entity == "Invoice" and r.to_entity == "Payable"]
        assert len(impl_edges) == 1
        steps = [log["step"] for log in pipeline.action_log]
        assert "2.1a_class_extends_interface" in steps

    def test_extends_target_derived_as_interface_after_the_loop_is_still_normalized(self):
        """2.10c: Base's kind is unset at loop-start (Sub->Base initially lands in
        class_parent_map, a perfectly normal class hierarchy), and only becomes an
        interface via 2.6's post-loop derivation (2+ children + semantic signals).
        The stale class_parent_map entry must be cleaned up even though 2.1a never
        saw it as a mismatch."""
        entities = [
            SketchEntity(name="Payable", kind="supporting", note="", extends="Base"),
            SketchEntity(name="Refundable", kind="supporting", note="", extends="Base"),
            SketchEntity(name="Base", kind="supporting", note=""),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=[])
        pipeline = make_pipeline()
        from src.schemas.blueprint import FeatureToggle
        pipeline.preset.oop.interface = FeatureToggle(enabled=True)
        result = pipeline.repair(sketch)

        base = next(e for e in result.entities if e.name == "Base")
        assert base.is_interface is True
        for name in ("Payable", "Refundable"):
            e = next(x for x in result.entities if x.name == name)
            assert e.extends is None
        steps = [log["step"] for log in pipeline.action_log]
        assert "2.10c_class_extends_became_interface" in steps


class TestInterfaceDeduction:
    def test_interface_deduced(self):
        # We rename A to "Payable" and give it kind="supporting" to pass the semantic gate
        entities = [
            SketchEntity(name="Payable", kind="supporting", note="Can be paid"),
            SketchEntity(name="B", kind="core", note="", extends="Payable"),
            SketchEntity(name="C", kind="core", note="", extends="Payable")
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=[])
        
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
        inheritance_targets = {e.extends for e in result.entities if e.extends} | {
            p for e in result.entities for p in e.extends_interfaces
        }
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
        """'Inheritor' here means B, a class, points at Payable via `extends` while
        Payable is already an interface. repair_pipeline's 2.1a normalizes that
        mismatch to an 'implements' edge (a class extending an interface isn't valid
        Java), but Payable must still count as having an implementer either way - not
        demoted."""
        entities = [
            SketchEntity(name="B", kind="core", note="", extends="Payable"),
            SketchEntity(name="Payable", kind="supporting", note="", is_interface=True),
        ]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=[])

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

        # Build graph from the final extends/extends_interfaces state - inheritance no
        # longer lives in relationships (see the Tầng 0 schema change).
        adj = {e.name: [] for e in result.entities}
        for e in result.entities:
            if e.extends and e.extends in adj:
                adj[e.name].append(e.extends)
            for p in e.extends_interfaces:
                if p in adj:
                    adj[e.name].append(p)

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
    def test_repaired_output_never_has_class_extending_an_interface(self, sketch):
        """The old 'single parent' fuzz test is now vacuous - SketchEntity.extends is
        a single Optional[str], so two parents for one class is unrepresentable by
        construction (Tầng 0 schema change), nothing left to fuzz there. What repair
        still has to guarantee under fuzzing is the cross-entity invariant the schema
        can't enforce on its own: a class's `extends` target must never be an
        interface (GAP-2), and an interface's `extends_interfaces` targets must always
        be interfaces (GAP-3) - including after 2.6/2.6b derives a kind mid-repair
        (2.10c's job)."""
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        by_name = {e.name: e for e in result.entities}
        for e in result.entities:
            if not e.is_interface and e.extends:
                target = by_name.get(e.extends)
                assert target is not None and not target.is_interface, (
                    f"{e.name} (class) extends {e.extends}, which is an interface"
                )
            if e.is_interface:
                for p in e.extends_interfaces:
                    target = by_name.get(p)
                    assert target is not None and target.is_interface, (
                        f"{e.name} (interface) extends_interfaces includes {p}, which is not an interface"
                    )

    @given(sketch_strategy())
    def test_no_entity_lost_or_duplicated(self, sketch):
        pipeline = make_pipeline()
        result = pipeline.repair(sketch)
        
        original_names = {e.name.lower() for e in sketch.entities}
        result_names = {e.name.lower() for e in result.entities}
        
        # We don't add entities in this phase, so result MUST be exactly equal 
        # to the deduplicated original names.
        assert result_names == original_names
