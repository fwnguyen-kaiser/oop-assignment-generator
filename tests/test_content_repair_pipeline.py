from src.schemas.java_ast import JavaClass, JavaField, JavaMethod, JavaParameter, JavaTypeRef
from src.validator.content_repair_pipeline import ContentRepairPipeline, default_body_for_return_type, method_signature


def _field(name, type_name="double"):
    return JavaField(modifier="private", type_ref=JavaTypeRef(name=type_name, is_collection=False), name=name)


def _method(name, params=None, return_type="void", body="System.out.println();", is_abstract=False):
    return JavaMethod(
        modifier="public",
        return_type=JavaTypeRef(name=return_type, is_collection=False) if return_type != "void" else None,
        name=name,
        parameters=params or [],
        body=body,
        is_abstract=is_abstract,
    )


def test_dedupe_field_same_class():
    cls = JavaClass(name="Account", fields=[_field("balance"), _field("balance")], methods=[])
    result = ContentRepairPipeline().repair([cls])
    assert len(result[0].fields) == 1


def test_dedupe_field_shadowing_ancestor():
    parent = JavaClass(name="Account", fields=[_field("balance")], methods=[])
    child = JavaClass(name="Checking", extends="Account", fields=[_field("balance"), _field("overdraft")], methods=[])
    result = ContentRepairPipeline().repair([parent, child])
    child_result = next(c for c in result if c.name == "Checking")
    assert [f.name for f in child_result.fields] == ["overdraft"]


def test_accessor_collision_drops_llm_method_reproduces_real_bug():
    """Reproduces the real BankAccount.getBalance() duplicate found in output/java_detailed."""
    cls = JavaClass(
        name="BankAccount",
        fields=[_field("balance")],
        methods=[_method("getBalance", return_type="double", body="return this.balance;")],
    )
    result = ContentRepairPipeline().repair([cls])
    assert result[0].methods == []


def test_method_self_collision_within_class():
    cls = JavaClass(
        name="Weapon",
        fields=[],
        methods=[_method("attack", body="return;"), _method("attack", body="System.out.println(1);")],
    )
    result = ContentRepairPipeline().repair([cls])
    assert len(result[0].methods) == 1


def test_invalid_override_renamed():
    parent = JavaClass(name="Account", fields=[], methods=[_method("summary", return_type="String", body="return null;")])
    child = JavaClass(name="Checking", extends="Account", fields=[], methods=[_method("summary", return_type="int", body="return 0;")])
    result = ContentRepairPipeline().repair([parent, child])
    child_result = next(c for c in result if c.name == "Checking")
    assert child_result.methods[0].name == "summaryImpl"


def test_reserved_keyword_renamed():
    cls = JavaClass(name="Item", fields=[_field("class", type_name="String")], methods=[])
    result = ContentRepairPipeline().repair([cls])
    assert result[0].fields[0].name == "class_"


def test_find_missing_contract_method_reproduces_real_bug():
    """Reproduces the real SavingsAccount not implementing InterestBearing.calculateInterest()."""
    interface = JavaClass(
        name="InterestBearing",
        is_interface=True,
        fields=[],
        methods=[_method("calculateInterest", return_type="double", body=None, is_abstract=True)],
    )
    savings = JavaClass(
        name="SavingsAccount",
        implements=["InterestBearing"],
        fields=[],
        methods=[_method("applyInterest", body="System.out.println(1);")],
    )
    missing = ContentRepairPipeline().find_missing_contract_methods([interface, savings])
    assert "SavingsAccount" in missing
    assert missing["SavingsAccount"][0].name == "calculateInterest"


def test_find_missing_contract_method_not_flagged_when_fulfilled():
    interface = JavaClass(
        name="InterestBearing",
        is_interface=True,
        fields=[],
        methods=[_method("calculateInterest", return_type="double", body=None, is_abstract=True)],
    )
    savings = JavaClass(
        name="SavingsAccount",
        implements=["InterestBearing"],
        fields=[],
        methods=[_method("calculateInterest", return_type="double", body="return this.rate;")],
    )
    missing = ContentRepairPipeline().find_missing_contract_methods([interface, savings])
    assert missing == {}


def test_find_missing_contract_method_fulfilled_by_ancestor():
    interface = JavaClass(name="Movable", is_interface=True, fields=[], methods=[_method("move", body=None, is_abstract=True)])
    base = JavaClass(name="Entity", implements=["Movable"], fields=[], methods=[_method("move", body="this.x += 1;")])
    leaf = JavaClass(name="Player", extends="Entity", fields=[], methods=[])
    missing = ContentRepairPipeline().find_missing_contract_methods([interface, base, leaf])
    assert missing == {}


def test_abstract_class_not_flagged_itself():
    interface = JavaClass(name="Movable", is_interface=True, fields=[], methods=[_method("move", body=None, is_abstract=True)])
    base = JavaClass(name="Entity", is_abstract=True, implements=["Movable"], fields=[], methods=[])
    missing = ContentRepairPipeline().find_missing_contract_methods([interface, base])
    assert missing == {}


def test_accessor_collision_with_inherited_field_dropped():
    """Reproduces the real javac error: SavingsAccount.getBalance() returning String
    is an invalid override of Account's auto-generated 'double getBalance()'."""
    account = JavaClass(name="Account", fields=[_field("balance")], methods=[])
    savings = JavaClass(
        name="SavingsAccount",
        extends="Account",
        fields=[],
        methods=[_method("getBalance", return_type="String", body="return String.valueOf(this.balance);")],
    )
    result = ContentRepairPipeline().repair([account, savings])
    savings_result = next(c for c in result if c.name == "SavingsAccount")
    assert savings_result.methods == []


def test_inherited_private_field_access_promotes_to_protected():
    """Reproduces the real javac error: 'balance has private access in Account' when a
    subclass method body touches an inherited field directly instead of via its getter."""
    account = JavaClass(name="Account", fields=[_field("balance")], methods=[])
    savings = JavaClass(
        name="SavingsAccount",
        extends="Account",
        fields=[],
        methods=[_method("addInterest", body="this.balance += 10;")],
    )
    result = ContentRepairPipeline().repair([account, savings])
    account_result = next(c for c in result if c.name == "Account")
    assert account_result.fields[0].modifier == "protected"


def test_bare_field_access_without_this_prefix_also_promotes():
    """Reproduces the real javac error when the LLM omits 'this.' - still valid Java,
    but the earlier 'this.'-only regex missed it entirely."""
    account = JavaClass(name="Account", fields=[_field("balance")], methods=[])
    savings = JavaClass(
        name="SavingsAccount",
        extends="Account",
        fields=[],
        methods=[_method("addInterest", body="balance += 10;")],
    )
    result = ContentRepairPipeline().repair([account, savings])
    account_result = next(c for c in result if c.name == "Account")
    assert account_result.fields[0].modifier == "protected"


def test_bare_field_access_shadowed_by_parameter_not_falsely_promoted():
    """A same-named parameter should shadow the field - the bare identifier refers to the
    parameter, not the inherited field, so no promotion should happen."""
    account = JavaClass(name="Account", fields=[_field("balance")], methods=[])
    savings = JavaClass(
        name="SavingsAccount",
        extends="Account",
        fields=[],
        methods=[_method("setBalanceTwice", params=[JavaParameter(type_ref=JavaTypeRef(name="double"), name="balance")], body="System.out.println(balance);")],
    )
    result = ContentRepairPipeline().repair([account, savings])
    account_result = next(c for c in result if c.name == "Account")
    assert account_result.fields[0].modifier == "private"


def test_own_field_access_not_affected_by_protected_promotion():
    cls = JavaClass(name="Account", fields=[_field("balance")], methods=[_method("reset", body="this.balance = 0;")])
    result = ContentRepairPipeline().repair([cls])
    assert result[0].fields[0].modifier == "private"


def test_method_signature_distinguishes_overloads():
    """method_signature() is the canonical JLS method identity (name + parameter types,
    NOT name alone) - two overloads sharing a name must produce different tuples, or
    every dict/set keyed off it (e.g. src/detail_pipeline.py's fill_map) would silently
    collide two different methods into one."""
    m1 = _method("calculate", params=[JavaParameter(type_ref=JavaTypeRef(name="int"), name="x")], return_type="int", body="return x;")
    m2 = _method("calculate", params=[JavaParameter(type_ref=JavaTypeRef(name="double"), name="x")], return_type="double", body="return x;")
    assert method_signature(m1) != method_signature(m2)
    assert method_signature(m1) == ("calculate", ("int",))
    assert method_signature(m2) == ("calculate", ("double",))


def test_fill_map_keyed_by_full_signature_disambiguates_overloads():
    """Regression test for a real bug: src/detail_pipeline.py used to key its
    class_name/method_name -> body merge dict by (class_name, method_name) alone, which
    silently collides two overloaded methods sharing a name (e.g. calculate(int) and
    calculate(double)) into a single dict entry - the LAST fill in the response list
    would silently overwrite the body for BOTH methods. This reproduces that exact
    merge pattern (now keyed by the full method_signature() tuple, matching
    src/detail_pipeline.py's actual fix) and proves each overload gets its own,
    correct body instead of one clobbering the other."""
    int_overload = _method("calculate", params=[JavaParameter(type_ref=JavaTypeRef(name="int"), name="x")], return_type="int", body=None)
    double_overload = _method("calculate", params=[JavaParameter(type_ref=JavaTypeRef(name="double"), name="x")], return_type="double", body=None)

    # Simulates ContractFill entries the LLM would return, each echoing back its own param_types.
    fake_fills = [
        {"class_name": "Calc", "method_name": "calculate", "param_types": ["int"], "body": "return x * 2;"},
        {"class_name": "Calc", "method_name": "calculate", "param_types": ["double"], "body": "return x * 2.5;"},
    ]
    fill_map = {(f["class_name"], f["method_name"], tuple(f["param_types"])): f["body"] for f in fake_fills}

    int_overload.body = fill_map.get(("Calc",) + method_signature(int_overload))
    double_overload.body = fill_map.get(("Calc",) + method_signature(double_overload))

    assert int_overload.body == "return x * 2;"
    assert double_overload.body == "return x * 2.5;"
    # The whole point: they must NOT have ended up with the same body.
    assert int_overload.body != double_overload.body


def test_default_body_for_return_type():
    assert default_body_for_return_type(JavaTypeRef(name="int")) == "return 0;"
    assert default_body_for_return_type(JavaTypeRef(name="boolean")) == "return false;"
    assert default_body_for_return_type(JavaTypeRef(name="String")) == "return null;"
    assert default_body_for_return_type(None) is None
