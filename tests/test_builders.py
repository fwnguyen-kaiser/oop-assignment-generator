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
