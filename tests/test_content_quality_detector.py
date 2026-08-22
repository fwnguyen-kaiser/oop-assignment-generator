from src.schemas.java_ast import JavaClass, JavaField, JavaMethod, JavaTypeRef
from src.validator.content_quality_detector import find_content_quality_smells


def _field(name, type_name="double"):
    return JavaField(modifier="private", type_ref=JavaTypeRef(name=type_name, is_collection=False), name=name)


def _method(name, body, return_type="boolean"):
    return JavaMethod(
        modifier="public",
        return_type=JavaTypeRef(name=return_type) if return_type else None,
        name=name,
        parameters=[],
        body=body,
    )


def test_flags_bare_boolean_literal_referencing_no_fields():
    cls = JavaClass(name="Loan", fields=[_field("dueDate", "LocalDate")], methods=[
        _method("isOverdue", "return false;"),
    ])
    smells = find_content_quality_smells([cls])
    assert "Loan" in smells
    assert smells["Loan"][0].method.name == "isOverdue"


def test_does_not_flag_boolean_method_that_references_a_field():
    cls = JavaClass(name="Loan", fields=[_field("dueDate", "LocalDate")], methods=[
        _method("isOverdue", "return this.dueDate.isBefore(LocalDate.now());"),
    ])
    assert find_content_quality_smells([cls]) == {}


def test_does_not_flag_boolean_literal_when_class_has_no_fields_at_all():
    """A genuinely stateless class returning a fixed boolean isn't necessarily a smell -
    scoped narrowly to avoid false positives on legitimately field-less classes."""
    cls = JavaClass(name="Utility", fields=[], methods=[_method("alwaysTrue", "return true;")])
    assert find_content_quality_smells([cls]) == {}


def test_does_not_flag_a_body_with_real_conditional_logic():
    cls = JavaClass(name="Loan", fields=[_field("dueDate", "LocalDate")], methods=[
        _method("isOverdue", "if (this.dueDate == null) { return false; } return true;"),
    ])
    assert find_content_quality_smells([cls]) == {}


def test_flags_hardcoded_date_literal_ignoring_the_class_date_field():
    """Live-observed pattern: Member.checkoutItem() hardcoded a due-date string
    instead of engaging with the class's own dueDate field at all (e.g. LocalDate.now()
    or the field itself)."""
    cls = JavaClass(name="Member", fields=[_field("dueDate", "LocalDate")], methods=[
        JavaMethod(modifier="public", return_type=None, name="checkoutItem", parameters=[],
                   body='System.out.println(LocalDate.parse("2023-12-31"));'),
    ])
    smells = find_content_quality_smells([cls])
    assert "Member" in smells
    assert smells["Member"][0].method.name == "checkoutItem"


def test_does_not_flag_date_literal_when_body_references_a_date_field():
    """The check is "ignores every date-typed field on the class", not "ignores this
    ONE specific field" - deliberately coarse (same spirit as 4.1/4.8's own scope
    decisions) to avoid needing to guess WHICH field a given literal "should" have
    used, only whether the method engages with the class's date state at all."""
    cls = JavaClass(name="Member", fields=[_field("dueDate", "LocalDate")], methods=[
        JavaMethod(modifier="public", return_type=None, name="checkoutItem", parameters=[],
                   body='this.dueDate = LocalDate.parse("2023-12-31");'),
    ])
    assert find_content_quality_smells([cls]) == {}


def test_does_not_flag_date_literal_when_class_has_no_date_typed_field():
    cls = JavaClass(name="Product", fields=[_field("sku", "String")], methods=[
        JavaMethod(modifier="public", return_type=None, name="log", parameters=[], body='System.out.println("2023-12-31");'),
    ])
    assert find_content_quality_smells([cls]) == {}
