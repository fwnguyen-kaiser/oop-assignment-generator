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
            # Drawn, not hardcoded: rule 2.8 has a separate branch for non-core entities,
            # and a fixture that only ever emits "core" silently prevents the very rule it
            # is meant to exercise.
            kind=draw(st.sampled_from(["core", "supporting"])),
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


def _pipeline(max_classes, aggregation_enabled=True):
    preset = BlueprintPreset(
        difficulty="test",
        structure=StructureConfig(classes=ClassCountConfig(min=1, max=max_classes)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=3),
            abstraction=FeatureToggle(enabled=True),
            interface=FeatureToggle(enabled=True),
            composition=FeatureToggle(enabled=True),
            aggregation=FeatureToggle(enabled=aggregation_enabled),
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


def _assert_repaired_output_compiles(sketch: SketchPlan, max_classes: int, aggregation_enabled=True):
    repaired = _pipeline(max_classes, aggregation_enabled).repair(sketch)
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


@st.composite
def pathological_sketch_strategy(draw):
    """Topologies the uniform-random strategy above almost never produces.

    Measured, not guessed: a coverage probe over 50 trials of `sketch_strategy` fired 13 of
    the 29 rule IDs, and rule `2.3` (depth) fired ZERO times - drawing one random parent per
    entity out of at most 6 names essentially never builds a chain longer than max_depth=3,
    and never closes an inheritance cycle either. So the shapes that exercise `2.1c`/`2.1d`
    (cycle detection) and `2.3` (depth) have to be constructed deliberately rather than
    waited for.
    """
    names = draw(st.lists(
        st.from_regex(r"^[A-Z][a-zA-Z0-9]{0,6}$", fullmatch=True),
        min_size=3, max_size=7, unique=True,
    ))
    topology = draw(st.sampled_from(["chain", "cycle", "star"]))

    parents = {}
    for i, n in enumerate(names):
        if topology == "chain":
            # Depth == len(names) - 1, so max_depth=3 is breached from 5 names up.
            parents[n] = names[i - 1] if i > 0 else None
        elif topology == "cycle":
            parents[n] = names[i - 1] if i > 0 else names[-1]
        else:
            parents[n] = names[0] if i > 0 else None

    entities = []
    for n in names:
        kind = draw(st.sampled_from(KINDS))
        entities.append(SketchEntity(
            name=n, kind=draw(st.sampled_from(["core", "supporting"])), note="fuzz",
            is_abstract=(kind == "abstract"),
            is_interface=(kind == "interface"),
            extends=parents[n],
            extends_interfaces=([parents[n]] if kind == "interface" and parents[n] else []),
        ))

    edges = draw(st.lists(
        st.tuples(st.sampled_from(names), st.sampled_from(names), st.sampled_from(EDGE_TYPES)),
        min_size=0, max_size=8,
    ))
    rels = [SketchRelationship(from_entity=f, to_entity=t, type=ty) for f, t, ty in edges]
    return SketchPlan(design_rationale="fuzz", entities=entities, relationships=rels)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pathological_sketch_strategy())
def test_chains_and_cycles_still_compile_after_repair(sketch):
    """Deep chains and closed inheritance cycles are the inputs rules 2.1c/2.1d/2.3 exist
    for; a class cap of 10 keeps 2.5 from masking them by deleting the chain instead."""
    _assert_repaired_output_compiles(sketch, max_classes=10)


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sketch_strategy())
def test_repaired_graph_compiles_with_aggregation_disabled(sketch):
    """Rule `2.7_aggregation_fallback` (retype aggregation edges to composition when the
    preset forbids aggregation) never fired in the coverage probe for a mundane reason: the
    fixture had aggregation enabled. A fixture that prevents the condition its rule reacts to
    is the same root-cause class as a validity check that runs before the mutation it guards -
    it reads as coverage and is not."""
    _assert_repaired_output_compiles(sketch, max_classes=10, aggregation_enabled=False)


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(sketch_strategy(), st.from_regex(r"^[A-Z][a-zA-Z0-9]{0,6}$", fullmatch=True))
def test_dangling_relationship_targets_are_repaired_not_rendered(sketch, ghost):
    """Rule `2.0_dangling` cannot fire while every edge target is sampled from the entity
    list by construction. Real Phase 1 output is LLM-generated and validate_sketch rejects
    dangling refs before repair sees them - but repair carries its own dangling sweep, and an
    unexercised safety net is an assumption."""
    if any(e.name == ghost for e in sketch.entities):
        return
    sketch.relationships.append(
        SketchRelationship(from_entity=sketch.entities[0].name, to_entity=ghost, type="association")
    )
    _assert_repaired_output_compiles(sketch, max_classes=10)
