from src.schemas.java_ast import JavaClass, JavaField, JavaTypeRef
from src.builders.java_builder import JavaBuilder

def test_abstract_class_gets_protected_constructor():
    bank_account = JavaClass(name="BankAccount", is_abstract=True, is_interface=False,
        extends=None, implements=[],
        fields=[JavaField(modifier="private", type_ref=JavaTypeRef(name="double", is_collection=False), name="balance")],
        methods=[])
    checking = JavaClass(name="CheckingAccount", is_abstract=False, is_interface=False,
        extends="BankAccount", implements=[], fields=[], methods=[])

    class_map = {"BankAccount": bank_account, "CheckingAccount": checking}
    builder = JavaBuilder()

    parent_code = builder.render_class(bank_account, class_map)
    assert "protected BankAccount(double balance)" in parent_code

    child_code = builder.render_class(checking, class_map)
    assert "super(balance)" in child_code
    print("[SUCCESS] test_abstract_class_gets_protected_constructor passed!")
    
if __name__ == "__main__":
    test_abstract_class_gets_protected_constructor()
