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


def test_missing_override_batch_fills_every_missing_method_not_just_the_first():
    """Live-found bug: javac only reports the FIRST missing-override method per class
    at a time, one per recompile - the old fix action added exactly one stub per
    round, so a class missing N methods across multiple interfaces needed N full
    Tier-1 rounds to close, and the default max_tier1=3 cap left a 4th missing method
    unfixed, shipping a non-compiling class. Verified live: 4 interfaces each
    requiring one distinct method, max_tier1=3, must still converge in one round by
    batch-filling the FULL contract gap (not just the one method javac happened to
    name), reusing content_repair_pipeline's own required-contract computation."""
    ifaces = [
        JavaClass(name=f"Iface{letter}", is_interface=True, methods=[
            JavaMethod(modifier="public", return_type=JavaTypeRef(name="int"), name=f"method{letter}", parameters=[], body=None, is_abstract=True),
        ])
        for letter in "ABCD"
    ]
    impl = JavaClass(name="Impl", implements=[f"Iface{l}" for l in "ABCD"], methods=[])
    gate = CompileVerificationGate(JavaBuilder())
    result = gate.verify_and_repair(ifaces + [impl], domain=None, provider=None, max_tier1=3, max_tier2=0)

    assert any("Tier 1: compiled successfully" in line for line in gate.report)
    impl_final = next(c for c in result if c.name == "Impl")
    assert {m.name for m in impl_final.methods} == {"methodA", "methodB", "methodC", "methodD"}


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
