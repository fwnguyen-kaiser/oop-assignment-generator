import pytest
from src.schemas.java_ast import JavaClass, JavaMethod, JavaTypeRef
from src.validator.compile_gate import CompileVerificationGate, is_javac_available, parse_errors
from src.builders.java_builder import JavaBuilder

pytestmark = pytest.mark.skipif(not is_javac_available(), reason="javac not on PATH")


def test_invalid_override_regex_matches_cannot_implement_not_just_cannot_override():
    """Live-found bug: javac uses a DIFFERENT verb for a return-type mismatch against
    an interface method ("cannot implement") than against a superclass method ("cannot
    override") - the regex only matched the latter, so a class implementing an
    interface with a mismatched return type fell through Tier 1 as an unrecognized
    error and was never fixed."""
    stderr = (
        "Child.java:8: error: getValue() in Child cannot implement getValue() in Base\n"
        "    public String getValue() {\n"
        "                  ^\n"
        "  return type String is not compatible with boolean\n"
        "1 error\n"
    )
    errors = parse_errors(stderr)
    assert len(errors) == 1
    assert errors[0]["kind"] == "invalid_override"
    assert errors[0]["child"] == "Child"
    assert errors[0]["parent"] == "Base"


def test_end_to_end_interface_return_type_mismatch_is_resolved_by_tier1():
    """The regex fix must actually close the real javac failure, not just match text -
    verified by running the full gate against real javac. Before the fix, this
    scenario fell through unresolved and shipped a non-compiling Child.java."""
    base = JavaClass(name="Base", is_interface=True, methods=[
        JavaMethod(modifier="public", return_type=JavaTypeRef(name="boolean"), name="check", parameters=[], body=None, is_abstract=True),
    ])
    child = JavaClass(name="Child", implements=["Base"], methods=[
        JavaMethod(modifier="public", return_type=JavaTypeRef(name="String"), name="check", parameters=[], body="return null;"),
    ])
    gate = CompileVerificationGate(JavaBuilder())
    result = gate.verify_and_repair([base, child], domain=None, provider=None, max_tier1=3, max_tier2=0)

    assert any("Tier 1: compiled successfully" in line for line in gate.report)
    child_final = next(c for c in result if c.name == "Child")
    assert any(m.name == "check" and m.return_type.name == "boolean" for m in child_final.methods)
