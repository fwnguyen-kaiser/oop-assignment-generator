import re
from typing import Dict, List, Optional
from src.schemas.java_ast import JavaClass, JavaMethod, JavaTypeRef
from src.builders.java_builder import JavaBuilder

JAVA_RESERVED_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private", "protected", "public",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while", "true", "false",
    "null", "var", "record", "yield",
}


def default_body_for_return_type(return_type: Optional[JavaTypeRef]) -> Optional[str]:
    if not return_type:
        return None
    name = return_type.name
    if name == "void":
        return None
    if name in ["int", "short", "long", "byte", "double", "float"]:
        return "return 0;"
    if name == "boolean":
        return "return false;"
    if name == "char":
        return "return '\\0';"
    return "return null;"


def _signature(method: JavaMethod) -> tuple:
    return (method.name, tuple(p.type_ref.name for p in method.parameters))


class ContentRepairPipeline:
    """Phase 4.5 — deterministic repair of LLM-invented fields/methods (Taxonomy A)
    plus detection of unfulfilled interface/abstract-method contracts (Taxonomy B)."""

    def __init__(self):
        self.action_log = []
        self._builder = JavaBuilder()

    def log_action(self, step: str, node: str, detail: str, action: str):
        self.action_log.append({"step": step, "node": node, "detail": detail, "action": action})

    def _sanitize_identifier(self, name: str, class_name: str, kind: str) -> str:
        if name in JAVA_RESERVED_KEYWORDS:
            fixed = name + "_"
            self.log_action("4.6_reserved_keyword", class_name, f"{kind} '{name}' is a Java reserved keyword", f"renamed to {fixed}")
            return fixed
        return name

    def _get_inherited_methods(self, class_name: str, class_map: Dict[str, JavaClass]) -> List[JavaMethod]:
        methods = []
        cls = class_map.get(class_name)
        if not cls or not cls.extends:
            return methods
        parent = class_map.get(cls.extends)
        if parent:
            methods.extend(self._get_inherited_methods(parent.name, class_map))
            methods.extend(parent.methods)
        return methods

    def repair(self, ast_classes: List[JavaClass]) -> List[JavaClass]:
        class_map = {c.name: c for c in ast_classes}

        for cls in ast_classes:
            # 4.6 Reserved keyword sanitization
            for f in cls.fields:
                f.name = self._sanitize_identifier(f.name, cls.name, "field")
            for m in cls.methods:
                m.name = self._sanitize_identifier(m.name, cls.name, "method")
                for p in m.parameters:
                    p.name = self._sanitize_identifier(p.name, cls.name, "parameter")

            # 4.0a Dedupe fields within the same class (case-insensitive, first occurrence wins)
            seen_field_names = set()
            deduped_fields = []
            for f in cls.fields:
                key = f.name.lower()
                if key in seen_field_names:
                    self.log_action("4.0a_dedupe_field", cls.name, f"duplicate field name '{f.name}'", "dropped duplicate")
                    continue
                seen_field_names.add(key)
                deduped_fields.append(f)
            cls.fields = deduped_fields

            # 4.0b Dedupe fields that shadow an inherited field
            inherited_names = {f.name.lower() for f in self._builder.get_inherited_fields(cls.name, class_map)}
            final_fields = []
            for f in cls.fields:
                if f.name.lower() in inherited_names:
                    self.log_action("4.0b_dedupe_inherited_field", cls.name, f"field '{f.name}' shadows an inherited field", "dropped duplicate")
                    continue
                final_fields.append(f)
            cls.fields = final_fields

            # 4.1 Drop methods colliding with auto-generated getters/setters
            # (own fields AND inherited fields - a subclass accessor override is redundant
            # since the ancestor's auto-generated one already exists, and if the LLM got the
            # return type wrong it would be an invalid override / compile error)
            inherited_fields_for_accessors = self._builder.get_inherited_fields(cls.name, class_map)
            auto_accessor_sigs = set()
            for f in list(cls.fields) + inherited_fields_for_accessors:
                capitalized = f.name[0].upper() + f.name[1:]
                auto_accessor_sigs.add((f"get{capitalized}", 0))
                auto_accessor_sigs.add((f"set{capitalized}", 1))

            filtered_methods = []
            for m in cls.methods:
                if (m.name, len(m.parameters)) in auto_accessor_sigs:
                    self.log_action("4.1_accessor_collision", cls.name, f"method '{m.name}' collides with an auto-generated accessor", "dropped LLM-invented method")
                    continue
                filtered_methods.append(m)
            cls.methods = filtered_methods

            # 4.2 Dedupe methods within the same class by (name, parameter types)
            seen_sigs = set()
            deduped_methods = []
            for m in cls.methods:
                sig = _signature(m)
                if sig in seen_sigs:
                    self.log_action("4.2_dedupe_method", cls.name, f"duplicate method signature '{m.name}({', '.join(sig[1])})'", "dropped duplicate")
                    continue
                seen_sigs.add(sig)
                deduped_methods.append(m)
            cls.methods = deduped_methods

        # 4.3 Rename methods that would be invalid overrides (same signature, different return type)
        for cls in ast_classes:
            inherited_by_sig = {_signature(m): m for m in self._get_inherited_methods(cls.name, class_map)}
            for m in cls.methods:
                sig = _signature(m)
                if sig in inherited_by_sig:
                    parent_m = inherited_by_sig[sig]
                    parent_ret = parent_m.return_type.name if parent_m.return_type else "void"
                    own_ret = m.return_type.name if m.return_type else "void"
                    if parent_ret != own_ret:
                        old_name = m.name
                        m.name = f"{m.name}Impl"
                        self.log_action("4.3_invalid_override", cls.name, f"method '{old_name}' conflicts with inherited signature (return {parent_ret} vs {own_ret})", f"renamed to {m.name}")

        # 4.8 Promote inherited fields to protected when a subclass method body accesses them
        # directly (e.g. `this.balance`). All fields are declared private by default, which is
        # correct Java visibility - but it means any LLM-written method body on a subclass that
        # touches an ancestor's field directly (instead of going through its getter/setter) is a
        # guaranteed compile error ("X has private access in Y"). Widening visibility is the only
        # deterministic fix that doesn't require rewriting/parsing the LLM's free-text method body.
        for cls in ast_classes:
            inherited_fields = self._builder.get_inherited_fields(cls.name, class_map)
            if not inherited_fields:
                continue
            inherited_by_name = {f.name: f for f in inherited_fields}
            for m in cls.methods:
                if not m.body:
                    continue
                param_names = {p.name for p in m.parameters}
                for field_name, field in inherited_by_name.items():
                    if field.modifier != "private":
                        continue
                    # Explicit "this.field" always unambiguously means the field itself.
                    explicit_access = re.search(rf'\bthis\.{re.escape(field_name)}\b', m.body)
                    # Bare "field" (no "this.") is also legal Java - but skip it if the method
                    # has a same-named parameter, since the bare identifier almost certainly
                    # refers to that parameter instead (classic setter shadowing pattern).
                    bare_access = field_name not in param_names and re.search(rf'\b{re.escape(field_name)}\b', m.body)
                    if explicit_access or bare_access:
                        field.modifier = "protected"
                        self.log_action("4.8_protected_promotion", cls.name, f"method '{m.name}' accesses inherited field '{field_name}' directly", f"promoted '{field_name}' to protected on its declaring class")

        return ast_classes

    def find_missing_contract_methods(self, ast_classes: List[JavaClass]) -> Dict[str, List[JavaMethod]]:
        """Taxonomy B detection: interface methods and abstract-declared methods that
        no class in the inheritance chain has actually implemented (body is not None)."""
        class_map = {c.name: c for c in ast_classes}
        missing: Dict[str, List[JavaMethod]] = {}

        for cls in ast_classes:
            if cls.is_interface or cls.is_abstract:
                continue

            chain = []
            cur = cls
            while cur:
                chain.append(cur)
                cur = class_map.get(cur.extends) if cur.extends else None

            required: Dict[tuple, JavaMethod] = {}
            for c in chain:
                for iface_name in c.implements:
                    iface = class_map.get(iface_name)
                    if iface and iface.is_interface:
                        for m in iface.methods:
                            required[_signature(m)] = m
                if c.is_abstract:
                    for m in c.methods:
                        if m.body is None:
                            required[_signature(m)] = m

            fulfilled = set()
            for c in chain:
                for m in c.methods:
                    if m.body is not None:
                        fulfilled.add(_signature(m))

            missing_methods = [m for key, m in required.items() if key not in fulfilled]
            if missing_methods:
                missing[cls.name] = missing_methods

        return missing
