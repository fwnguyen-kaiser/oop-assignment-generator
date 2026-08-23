"""Property-based fuzz of Phase 2 with the REAL javac as the oracle.

This replaces a claim that had no standing coverage behind it. The README used to cite
"380 adversarial fuzz trials round-tripped through real javac", which was a one-off
development-time measurement: the committed property tests in test_repair_pipeline.py
assert pure-PYTHON invariants (acyclicity, kind-consistency, no entity lost) and never
invoke a compiler, and their strategy builds plain classes only - never an interface.

Sampling an infinite graph space is L4 forever and no number of trials changes that. What
this does change is WHICH oracle the sampling is against: javac instead of a Python
re-statement of what we believe javac requires. A proxy oracle can only ever confirm our
own model of the rules - that is exactly how the interface-extends-interface bug survived.

Deliberately small max_examples: every trial spawns a real javac process.
"""
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from src.schemas.blueprint import (
    BlueprintPreset, ClassCountConfig, FeatureToggle, InheritanceConfig, OopConfig, StructureConfig,
)
from src.schemas.domain import DomainConfig
from src.schemas.logical_plan import SketchEntity, SketchPlan, SketchRelationship
from src.builders.java_builder import JavaBuilder
from src.pipeline import compile_logical_plan
from src.validator.compile_gate import compile_sources, is_javac_available
from src.validator.repair_pipeline import StructuralRepairPipeline

pytestmark = pytest.mark.skipif(not is_javac_available(), reason="javac not on PATH")

EDGE_TYPES = ["composition", "aggregation", "association", "implements"]
KINDS = ["class", "abstract", "interface"]


@st.composite
def sketch_strategy(draw):
    """Entity graphs that deliberately include interfaces and deliberately include
    illegal kind combinations (a class extending an interface, an interface extending a
    class, has-a edges out of an interface). Repairing those is Phase 2's whole job, so
    the fuzz has to be allowed to produce them."""
    names = draw(st.lists(
        st.from_regex(r"^[A-Z][a-zA-Z0-9]{0,6}$", fullmatch=True),
        min_size=2, max_size=6, unique=True,
    ))
    kinds = {n: draw(st.sampled_from(KINDS)) for n in names}

    entities = []
    for n in names:
        kind = kinds[n]
        parent = draw(st.one_of(st.none(), st.sampled_from(names)))
        entities.append(SketchEntity(
            name=n,
            kind="core",
            note="fuzz",
            is_abstract=(kind == "abstract"),
            is_interface=(kind == "interface"),
            # Assigned without regard to either side's kind on purpose - an illegal pair
            # here is an input Phase 2 must fix, not an input it is entitled to refuse.
            extends=(parent if parent != n else None),
            extends_interfaces=([parent] if kind == "interface" and parent and parent != n else []),
        ))

    edges = draw(st.lists(
        st.tuples(st.sampled_from(names), st.sampled_from(names), st.sampled_from(EDGE_TYPES)),
        min_size=0, max_size=10,
    ))
    rels = [SketchRelationship(from_entity=f, to_entity=t, type=ty) for f, t, ty in edges]
    return SketchPlan(design_rationale="fuzz", entities=entities, relationships=rels)


def _pipeline(max_classes):
    preset = BlueprintPreset(
        difficulty="test",
        structure=StructureConfig(classes=ClassCountConfig(min=1, max=max_classes)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=3),
            abstraction=FeatureToggle(enabled=True),
            interface=FeatureToggle(enabled=True),
            composition=FeatureToggle(enabled=True),
            aggregation=FeatureToggle(enabled=True),
        ),
    )
    domain = DomainConfig(name="fuzz", description="fuzz", keywords=[], style="fuzz")
    return StructuralRepairPipeline(preset, domain)


def _render(repaired: SketchPlan) -> dict:
    """The real production path, not a test-only renderer: compile_logical_plan ->
    build_ast -> render_class. A second renderer could silently diverge from the one that
    ships, which is precisely how the interface-extends-interface bug stayed hidden."""
    plan = compile_logical_plan(repaired, decisions=["fuzz"])
    builder = JavaBuilder()
    ast_classes = builder.build_ast(plan)
    class_map = {c.name: c for c in ast_classes}
    return {c.name: builder.render_class(c, class_map) for c in ast_classes}


def _assert_repaired_output_compiles(sketch: SketchPlan, max_classes: int):
    repaired = _pipeline(max_classes).repair(sketch)
    sources = _render(repaired)
    if not sources:
        return
    ok, stderr = compile_sources(sources)
    assert ok is True, (
        "Phase 2 produced a graph that real javac rejects.\n"
        + stderr
        + "\nRendered:\n"
        + "\n".join(f"--- {n} ---\n{c}" for n, c in sources.items())
    )


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sketch_strategy())
def test_repaired_graph_always_compiles(sketch):
    _assert_repaired_output_compiles(sketch, max_classes=10)


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sketch_strategy())
def test_repaired_graph_compiles_under_a_tight_class_cap(sketch):
    """max_classes=3 against a strategy that generates up to 6 entities, so rule 2.5
    (excess-class dropping) actually fires. The existing property tests use
    make_pipeline()'s default max_classes=10 with a max of 8 generated entities, so 2.5 is
    never exercised under fuzz there - and dropping an entity is the one repair action
    that can leave dangling references behind."""
    _assert_repaired_output_compiles(sketch, max_classes=3)
