from src.schemas.java_ast import JavaClass, JavaField, JavaTypeRef, JavaMethod
from src.schemas.logical_plan import LogicalPlan, SemanticEntity
from src.builders.java_builder import JavaBuilder
from src.builders.mermaid_builder import MermaidBuilder
from src.pipeline import compile_logical_plan
from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship


def test_abstract_method_on_abstract_class_renders_with_abstract_keyword():
    """Regression test for a real bug: an abstract method on a regular (non-interface)
    abstract class rendered as `public double foo(double x);` - missing the literal
    `abstract` keyword Java requires there, causing a genuine javac error ("missing
    method body, or declare abstract"). Found live via the real Gemini API once
    is_abstract started actually being set True (src/detail_pipeline.py's Phase
    5a-i/5a-ii split) - this test locks the fix in so it can't silently regress again,
    since nothing had ever exercised this render path before that."""
    cls = JavaClass(
        name="Product",
        is_abstract=True,
        methods=[
            JavaMethod(
                modifier="public",
                return_type=JavaTypeRef(name="double"),
                name="calculateDiscount",
                parameters=[],
                body=None,
                is_abstract=True,
            )
        ],
    )
    rendered = JavaBuilder().render_class(cls, {"Product": cls})
    assert "public abstract double calculateDiscount();" in rendered
    # No body block should be emitted for an abstract method - it must end in `;`,
    # never open a `{`.
    assert "calculateDiscount() {" not in rendered


def test_interface_method_does_not_get_a_redundant_abstract_keyword():
    """Interface methods are implicitly abstract (JLS SS9.4) - the renderer should NOT
    add the literal `abstract` keyword there, only for a regular abstract class's own
    abstract method (see test above). Both paths share the same `is_interface or
    m.is_abstract` condition in JavaBuilder.render_class, so this guards against a
    future edit accidentally adding "abstract" unconditionally for both cases."""
    cls = JavaClass(
        name="Shape",
        is_interface=True,
        methods=[
            JavaMethod(
                modifier="public",
                return_type=JavaTypeRef(name="double"),
                name="area",
                parameters=[],
                body=None,
                is_abstract=False,
            )
        ],
    )
    rendered = JavaBuilder().render_class(cls, {"Shape": cls})
    assert "public double area();" in rendered
    assert "abstract" not in rendered


def test_bigdecimal_field_gets_import_and_compiles_conceptually():
    """Reproduces the real 'cannot find symbol: class BigDecimal' javac failure."""
    cls = JavaClass(
        name="Invoice",
        fields=[JavaField(modifier="private", type_ref=JavaTypeRef(name="BigDecimal", is_collection=False), name="total")],
    )
    rendered = JavaBuilder().render_class(cls, {"Invoice": cls})
    assert "import java.math.BigDecimal;" in rendered


def test_bigint_field_gets_import():
    cls = JavaClass(
        name="Account",
        fields=[JavaField(modifier="private", type_ref=JavaTypeRef(name="BigInteger", is_collection=False), name="ledgerId")],
    )
    rendered = JavaBuilder().render_class(cls, {"Account": cls})
    assert "import java.math.BigInteger;" in rendered


def test_aggregation_routed_to_aggregates_with_not_composes_with():
    sketch = SketchPlan(
        design_rationale="test",
        entities=[
            SketchEntity(name="Library", kind="core", note=""),
            SketchEntity(name="Book", kind="core", note=""),
        ],
        relationships=[
            SketchRelationship(from_entity="Library", to_entity="Book", type="aggregation"),
        ],
    )
    plan = compile_logical_plan(sketch, ["test"])
    library = next(e for e in plan.domain_entities if e.name == "Library")
    assert library.aggregates_with == ["Book"]
    assert library.composes_with is None


def test_composition_still_routed_to_composes_with():
    sketch = SketchPlan(
        design_rationale="test",
        entities=[
            SketchEntity(name="Order", kind="core", note=""),
            SketchEntity(name="Product", kind="core", note=""),
        ],
        relationships=[
            SketchRelationship(from_entity="Order", to_entity="Product", type="composition"),
        ],
    )
    plan = compile_logical_plan(sketch, ["test"])
    order = next(e for e in plan.domain_entities if e.name == "Order")
    assert order.composes_with == ["Product"]
    assert order.aggregates_with is None


def test_interface_extending_interface_routes_to_extends_interfaces_not_implements():
    """Regression test for a real javac failure: an inheritance edge between two
    interfaces used to be routed into `implements` (compile_logical_plan only checked
    the TARGET's is_interface, not the SOURCE's), and JavaBuilder never renders
    `extends` for interfaces at all - so `interface Printable extends Readable`
    rendered as `public interface Printable implements Readable`, which javac rejects
    with "'{' expected". Found by probing all 4 (source-kind, target-kind) combinations
    for the `extends`/`implements` edge live against real javac. Since the Tầng 0
    schema change, interface-extends-interface is declared via the entity's own
    `extends_interfaces` field rather than a relationship at all."""
    sketch = SketchPlan(
        design_rationale="test",
        entities=[
            SketchEntity(name="Readable", kind="supporting", note="", is_interface=True),
            SketchEntity(name="Printable", kind="supporting", note="", is_interface=True, extends_interfaces=["Readable"]),
        ],
        relationships=[],
    )
    plan = compile_logical_plan(sketch, ["test"])
    printable = next(e for e in plan.support_entities if e.name == "Printable")
    assert printable.extends_interfaces == ["Readable"]
    assert printable.implements is None
    assert printable.inherits_from is None

    ast = JavaBuilder().build_ast(plan)
    rendered = JavaBuilder().render_class(next(c for c in ast if c.name == "Printable"), {c.name: c for c in ast})
    assert "public interface Printable extends Readable {" in rendered
    assert "implements" not in rendered


def test_interface_multiple_inheritance_survives_repair_pipeline_downgrade():
    """End-to-end regression: an interface extending SEVERAL other interfaces
    (`interface Printable extends Readable, Writable`) must survive repair and still
    compile. Before the Tầng 0 schema change, this relied on repair_pipeline's rule
    2.1 downgrading the second 'inheritance' edge to 'implements' and
    compile_logical_plan routing it back to extends_interfaces by kind. Now inheritance
    lives on SketchEntity.extends_interfaces directly (a list, since JLS allows an
    interface multiple parents) - repair_pipeline's iface_parent_map keeps ALL of them,
    no downgrade involved at all."""
    from src.schemas.blueprint import BlueprintPreset, StructureConfig, ClassCountConfig, OopConfig, InheritanceConfig, FeatureToggle
    from src.schemas.domain import DomainConfig
    from src.validator.repair_pipeline import StructuralRepairPipeline
    import subprocess, tempfile, os

    preset = BlueprintPreset(
        difficulty="d",
        structure=StructureConfig(classes=ClassCountConfig(min=1, max=10)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=3),
            interface=FeatureToggle(enabled=True),
        ),
    )
    domain = DomainConfig(name="d", description="d", keywords=[], style="x")

    sketch = SketchPlan(
        design_rationale="test",
        entities=[
            SketchEntity(name="Readable", kind="supporting", note="", is_interface=True),
            SketchEntity(name="Writable", kind="supporting", note="", is_interface=True),
            SketchEntity(name="Printable", kind="supporting", note="", is_interface=True, extends_interfaces=["Readable", "Writable"]),
            SketchEntity(name="Doc", kind="core", note=""),
        ],
        relationships=[
            SketchRelationship(from_entity="Doc", to_entity="Printable", type="implements"),
        ],
    )
    repaired = StructuralRepairPipeline(preset, domain).repair(sketch)
    plan = compile_logical_plan(repaired, ["test"])
    printable = next(e for e in plan.support_entities if e.name == "Printable")
    assert set(printable.extends_interfaces) == {"Readable", "Writable"}

    ast = JavaBuilder().build_ast(plan)
    m = {c.name: c for c in ast}
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for c in ast:
            p = os.path.join(d, f"{c.name}.java")
            with open(p, "w", encoding="utf-8") as f:
                f.write(JavaBuilder().render_class(c, m))
            paths.append(p)
        result = subprocess.run(["javac", "-d", d] + paths, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_aggregation_renders_hollow_diamond_in_diagram():
    plan = LogicalPlan(
        design_decisions=["test"],
        domain_entities=[
            SemanticEntity(name="Library", aggregates_with=["Book"]),
            SemanticEntity(name="Book"),
        ],
        support_entities=[],
    )
    diagram = MermaidBuilder().build_class_diagram(plan)
    assert "Library o-- Book" in diagram
    assert "Library *-- Book" not in diagram


def test_aggregation_field_present_in_ast():
    plan = LogicalPlan(
        design_decisions=["test"],
        domain_entities=[
            SemanticEntity(name="Library", aggregates_with=["Book"]),
            SemanticEntity(name="Book"),
        ],
        support_entities=[],
    )
    ast = JavaBuilder().build_ast(plan)
    library = next(c for c in ast if c.name == "Library")
    assert any(f.name == "books" and f.type_ref.name == "Book" for f in library.fields)


def test_composition_aggregation_association_combined_on_same_entity():
    """An entity can legitimately have all three relationship kinds at once (e.g. Order
    composes Product, aggregates Coupon, associates with Customer) - each must render with
    its own correct UML arrow and produce its own AST field, without any of the three
    clobbering the others."""
    plan = LogicalPlan(
        design_decisions=["test"],
        domain_entities=[
            SemanticEntity(name="Order", composes_with=["Product"], aggregates_with=["Coupon"], associated_with=["Customer"]),
            SemanticEntity(name="Product"),
            SemanticEntity(name="Coupon"),
            SemanticEntity(name="Customer"),
        ],
        support_entities=[],
    )
    diagram = MermaidBuilder().build_class_diagram(plan)
    assert "Order *-- Product" in diagram
    assert "Order o-- Coupon" in diagram
    assert "Order --> Customer" in diagram

    ast = JavaBuilder().build_ast(plan)
    order = next(c for c in ast if c.name == "Order")
    field_names = {f.name for f in order.fields}
    assert field_names == {"products", "coupons", "customer"}
    products_field = next(f for f in order.fields if f.name == "products")
    coupons_field = next(f for f in order.fields if f.name == "coupons")
    customer_field = next(f for f in order.fields if f.name == "customer")
    assert products_field.type_ref.is_collection is True
    assert coupons_field.type_ref.is_collection is True
    assert customer_field.type_ref.is_collection is False


def test_render_class_defers_to_an_explicit_method_matching_an_accessor_signature():
    """Live-found bug: JavaBuilder used to unconditionally generate a getter/setter
    for every private field regardless of whether the class already declares an
    explicit method with that exact (name, arity) - so a class whose field auto-
    generates `getValue()` AND separately has an explicit `getValue()` method (e.g.
    fulfilling an interface contract) rendered BOTH, a real javac 'method already
    defined' error, even when their signatures matched exactly."""
    cls = JavaClass(
        name="Child",
        implements=["Base"],
        fields=[JavaField(modifier="private", type_ref=JavaTypeRef(name="boolean"), name="value")],
        methods=[JavaMethod(modifier="public", return_type=JavaTypeRef(name="boolean"), name="getValue", parameters=[], body="return false;")],
    )
    rendered = JavaBuilder().render_class(cls, {"Child": cls})
    assert rendered.count("getValue()") == 1
    # setValue has no explicit collision, so the auto-generated setter must still exist.
    assert "setValue(boolean value)" in rendered


def test_render_class_still_generates_accessor_when_no_explicit_method_collides():
    cls = JavaClass(
        name="Product",
        fields=[JavaField(modifier="private", type_ref=JavaTypeRef(name="double"), name="price")],
    )
    rendered = JavaBuilder().render_class(cls, {"Product": cls})
    assert "public double getPrice()" in rendered
    assert "public void setPrice(double price)" in rendered


def test_has_a_field_on_interface_renders_loudly_instead_of_silently_dropping():
    """An interface holding private mutable state is invalid Java - repair_pipeline.py
    strips this before rendering in the normal pipeline, so this should be unreachable
    there, but JavaBuilder itself used to silently skip ALL fields (and therefore the
    constructor/accessors too) whenever is_interface=True, which would hide the data
    loss with no error if this were ever reached some other way (e.g. calling
    compile_logical_plan directly). Rendering the field and letting javac reject it
    matches how `implements` is already handled for the equivalent case."""
    cls = JavaClass(
        name="BadInterface",
        is_interface=True,
        fields=[JavaField(modifier="private", type_ref=JavaTypeRef(name="int"), name="count")],
    )
    rendered = JavaBuilder().render_class(cls, {"BadInterface": cls})
    assert "private int count;" in rendered


class TestIdentifierSafety:
    """Independent-audit finding: JavaField/JavaMethod/JavaParameter.name had zero
    validation, unlike SketchEntity.name (PascalCase-validated since Phase 1). An
    unsafe name could both break the rendered .java file and flow unescaped into
    mermaid_builder.py's diagram output, which has no compiler-equivalent oracle to
    catch it."""

    def test_field_rejects_unsafe_names(self):
        import pytest
        for bad in ["bad name", "bad-name", "123bad", 'bad"quote', "bad<tag>", ""]:
            with pytest.raises(Exception):
                JavaField(modifier="private", type_ref=JavaTypeRef(name="int"), name=bad)

    def test_field_accepts_valid_identifier(self):
        f = JavaField(modifier="private", type_ref=JavaTypeRef(name="int"), name="balance_1")
        assert f.name == "balance_1"

    def test_method_rejects_unsafe_names(self):
        import pytest
        with pytest.raises(Exception):
            JavaMethod(modifier="public", return_type=None, name="bad method()", parameters=[], body=None)

    def test_signature_method_rejects_unsafe_names(self):
        import pytest
        from src.schemas.java_ast import SignatureMethod
        with pytest.raises(Exception):
            SignatureMethod(modifier="public", return_type=None, name="bad<name>", parameters=[])

    def test_reserved_keyword_is_still_accepted_here(self):
        """Rejecting Java keywords is deliberately NOT this validator's job - that's
        content_repair_pipeline.py's 4.6 rule, which renames rather than rejects.
        Adding a keyword blocklist here would double up with 4.6's friendlier fix."""
        f = JavaField(modifier="private", type_ref=JavaTypeRef(name="String"), name="class")
        assert f.name == "class"
