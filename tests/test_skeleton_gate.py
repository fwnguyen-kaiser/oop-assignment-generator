"""Regression net for JavaBuilder's class-declaration legality boundary: for every
(source kind, target kind) pair across {class, abstract, interface}, is `extends` /
`implements` between them legal Java? Labels are NOT hand-written - each cell is
decided by actually rendering a minimal SemanticEntity pair through JavaBuilder and
compiling the result with real javac (the same oracle SkeletonGate itself uses).

This is the regression net item #2 from the Phase 2 repair-layer audit: if a future
change to compile_logical_plan's routing or JavaBuilder's render_class silently
reintroduces a bad cell (e.g. the interface-extends-interface bug this session found),
this test catches it without anyone needing to remember the bug existed.
"""
import os
import shutil
import subprocess
import tempfile
import pytest
from src.schemas.logical_plan import SemanticEntity, LogicalPlan
from src.builders.java_builder import JavaBuilder
from src.validator.compile_gate import is_javac_available

pytestmark = pytest.mark.skipif(not is_javac_available(), reason="javac not on PATH")

KINDS = ["class", "abstract", "interface"]


def _entity(name: str, kind: str) -> SemanticEntity:
    return SemanticEntity(
        name=name,
        is_abstract=(kind == "abstract"),
        is_interface=(kind == "interface"),
    )


def _compile_pair(source: SemanticEntity, target: SemanticEntity) -> bool:
    plan = LogicalPlan(design_decisions=["matrix probe"], domain_entities=[source, target], support_entities=[])
    ast = JavaBuilder().build_ast(plan)
    class_map = {c.name: c for c in ast}
    d = tempfile.mkdtemp(prefix="matrix_")
    try:
        paths = []
        for c in ast:
            p = os.path.join(d, f"{c.name}.java")
            with open(p, "w", encoding="utf-8") as f:
                f.write(JavaBuilder().render_class(c, class_map))
            paths.append(p)
        result = subprocess.run(["javac", "-d", d] + paths, capture_output=True, text=True)
        return result.returncode == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# extends: legal per JLS iff (source, target) both non-interface, or both interface.
# class/abstract can never extend an interface; an interface can never extend a
# class/abstract. This is what compile_logical_plan's kind-aware routing (see
# src/pipeline.py::compile_logical_plan) and JavaBuilder.render_class both encode.
EXTENDS_LEGAL = {
    ("class", "class"), ("class", "abstract"),
    ("abstract", "class"), ("abstract", "abstract"),
    ("interface", "interface"),
}


@pytest.mark.parametrize("source_kind", KINDS)
@pytest.mark.parametrize("target_kind", KINDS)
def test_extends_matrix_cell(source_kind, target_kind):
    source = _entity("Source", source_kind)
    target = _entity("Target", target_kind)
    if source_kind == "interface":
        source.extends_interfaces = [target.name]
    else:
        source.inherits_from = target.name

    compiled = _compile_pair(source, target)
    expected = (source_kind, target_kind) in EXTENDS_LEGAL
    assert compiled == expected, (
        f"extends: {source_kind} -> {target_kind} expected "
        f"{'LEGAL' if expected else 'ILLEGAL'} but javac said {'LEGAL' if compiled else 'ILLEGAL'}"
    )


# implements: legal per JLS iff target is an interface and source is not an
# interface itself (interface-to-interface is `extends`, not `implements` - see
# EXTENDS_LEGAL/compile_logical_plan's routing above).
IMPLEMENTS_LEGAL = {
    ("class", "interface"), ("abstract", "interface"),
}


@pytest.mark.parametrize("source_kind", KINDS)
@pytest.mark.parametrize("target_kind", KINDS)
def test_implements_matrix_cell(source_kind, target_kind):
    source = _entity("Source", source_kind)
    target = _entity("Target", target_kind)
    source.implements = [target.name]

    compiled = _compile_pair(source, target)
    expected = (source_kind, target_kind) in IMPLEMENTS_LEGAL
    assert compiled == expected, (
        f"implements: {source_kind} -> {target_kind} expected "
        f"{'LEGAL' if expected else 'ILLEGAL'} but javac said {'LEGAL' if compiled else 'ILLEGAL'}"
    )


def test_gate_passes_on_a_realistic_repaired_sketch():
    """SkeletonGate.check() itself, not just the renderer, wired through the real
    repair_pipeline -> compile_logical_plan -> JavaBuilder path."""
    from src.schemas.blueprint import BlueprintPreset, StructureConfig, ClassCountConfig, OopConfig, InheritanceConfig, FeatureToggle
    from src.schemas.domain import DomainConfig
    from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship
    from src.validator.repair_pipeline import StructuralRepairPipeline
    from src.validator.skeleton_gate import SkeletonGate

    preset = BlueprintPreset(
        difficulty="d",
        structure=StructureConfig(classes=ClassCountConfig(min=1, max=10)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=3),
            interface=FeatureToggle(enabled=True),
            abstraction=FeatureToggle(enabled=True),
            composition=FeatureToggle(enabled=True),
            aggregation=FeatureToggle(enabled=True),
        ),
    )
    domain = DomainConfig(name="d", description="d", keywords=[], style="x")
    sketch = SketchPlan(
        design_rationale="test",
        entities=[
            SketchEntity(name="Payable", kind="supporting", note="", is_interface=True),
            SketchEntity(name="Invoice", kind="core", note=""),
            SketchEntity(name="Bill", kind="core", note=""),
        ],
        relationships=[
            SketchRelationship(from_entity="Invoice", to_entity="Payable", type="implements"),
            SketchRelationship(from_entity="Bill", to_entity="Invoice", type="association"),
        ],
    )
    repaired = StructuralRepairPipeline(preset, domain).repair(sketch)
    ok, stderr = SkeletonGate().check(repaired)
    assert ok, stderr
