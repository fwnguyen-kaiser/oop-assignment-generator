from src.schemas.domain import DomainConfig
from src.schemas.blueprint import BlueprintPreset, StructureConfig, ClassCountConfig, OopConfig, FeatureToggle, InheritanceConfig
from src.schemas.java_ast import JavaClass, JavaField, JavaTypeRef
from src.builders.markdown_builder import MarkdownBuilder


def _domain():
    return DomainConfig(name="Test Domain", description="A test domain.", keywords=[], style="formal")


def _preset(**oop_kwargs):
    return BlueprintPreset(
        difficulty="test",
        structure=StructureConfig(classes=ClassCountConfig(min=3, max=8)),
        oop=OopConfig(**oop_kwargs),
    )


def test_constraints_never_claim_interface_when_none_delivered():
    """Reproduces the real E-Commerce bug: preset says interface.enabled=True but the
    actual delivered classes had zero interfaces - the text must not lie about it."""
    preset = _preset(interface=FeatureToggle(enabled=True))
    ast_classes = [JavaClass(name="Product", is_interface=False, fields=[])]
    md = MarkdownBuilder().build_assignment(_domain(), preset, ast_classes, "classDiagram", {})
    assert "Must include at least one interface" not in md


def test_constraints_claim_interface_when_actually_delivered():
    preset = _preset(interface=FeatureToggle(enabled=True))
    ast_classes = [
        JavaClass(name="Product", is_interface=False, fields=[]),
        JavaClass(name="Taxable", is_interface=True, fields=[]),
    ]
    md = MarkdownBuilder().build_assignment(_domain(), preset, ast_classes, "classDiagram", {})
    assert "Must include at least one interface" in md


def test_constraints_reflect_abstract_and_composition_independently():
    preset = _preset()
    ast_classes = [
        JavaClass(name="Order", is_abstract=False, fields=[
            JavaField(modifier="private", type_ref=JavaTypeRef(name="Product", is_collection=True), name="products")
        ]),
        JavaClass(name="Product", is_abstract=False, fields=[]),
    ]
    md = MarkdownBuilder().build_assignment(_domain(), preset, ast_classes, "classDiagram", {})
    assert "Must use composition between classes" in md
    assert "Must include at least one abstract class" not in md
    assert "Must include at least one interface" not in md


def test_inheritance_depth_not_claimed_without_actual_extends():
    """preset.oop.inheritance.enabled=True alone must not be enough - some class must
    actually extend something, mirroring the has_interface fix for the same reason."""
    preset = _preset(inheritance=InheritanceConfig(enabled=True, max_depth=3))
    ast_classes = [JavaClass(name="Product", extends=None, fields=[])]
    md = MarkdownBuilder().build_assignment(_domain(), preset, ast_classes, "classDiagram", {})
    assert "Inheritance Depth" not in md


def test_design_decisions_rendered_when_present():
    preset = _preset()
    md = MarkdownBuilder().build_assignment(
        _domain(), preset, [JavaClass(name="Product", fields=[])], "classDiagram", {},
        design_decisions=["Chose composition over inheritance for flexibility."]
    )
    assert "## 3. Design Decisions" in md
    assert "Chose composition over inheritance for flexibility." in md


def test_design_decisions_section_omitted_when_absent():
    preset = _preset()
    md = MarkdownBuilder().build_assignment(
        _domain(), preset, [JavaClass(name="Product", fields=[])], "classDiagram", {}, design_decisions=None
    )
    assert "## 3. Design Decisions" not in md


def test_aggregation_only_not_misclaimed_as_composition():
    """Reproduces a real latent bug: composition and aggregation render as IDENTICAL
    JavaField shapes at the ast_classes level (both are just a private collection field),
    so the coarse AST-only heuristic cannot tell them apart and would wrongly claim
    'composition' for an entity that only ever had an aggregation relationship. The caller
    (detail_pipeline) must pass the accurate has_composition/has_aggregation from the
    LogicalPlan instead of relying on the AST-derived guess."""
    preset = _preset()
    ast_classes = [
        JavaClass(name="Library", fields=[
            JavaField(modifier="private", type_ref=JavaTypeRef(name="Book", is_collection=True), name="books")
        ]),
        JavaClass(name="Book", fields=[]),
    ]
    md = MarkdownBuilder().build_assignment(
        _domain(), preset, ast_classes, "classDiagram", {},
        has_composition=False, has_aggregation=True,
    )
    assert "Must use aggregation between classes" in md
    assert "Must use composition between classes" not in md


def test_ast_only_fallback_cannot_distinguish_and_over_claims_composition():
    """Documents the known limitation of the fallback path (no LogicalPlan available):
    it cannot tell aggregation from composition and defaults to claiming composition."""
    preset = _preset()
    ast_classes = [
        JavaClass(name="Library", fields=[
            JavaField(modifier="private", type_ref=JavaTypeRef(name="Book", is_collection=True), name="books")
        ]),
        JavaClass(name="Book", fields=[]),
    ]
    md = MarkdownBuilder().build_assignment(_domain(), preset, ast_classes, "classDiagram", {})
    assert "Must use composition between classes" in md


def test_skeletons_rendered_as_java_blocks():
    preset = _preset()
    md = MarkdownBuilder().build_assignment(
        _domain(), preset, [JavaClass(name="Product", fields=[])], "classDiagram",
        {"Product": "public class Product {}"}
    )
    assert "### Product.java" in md
    assert "public class Product {}" in md
