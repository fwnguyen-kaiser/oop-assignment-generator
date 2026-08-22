"""Content-quality smell detection - a DIFFERENT axis from content_repair_pipeline.py's
Taxonomy A rules. Those rules fix things that are decidably WRONG (a duplicate method
signature is never valid Java, full stop). Nothing here is ever "wrong" in that sense -
`return false;` is perfectly valid Java for a boolean method - so this can only ever
flag a SMELL (plausible sign the LLM wrote a lazy stub instead of real domain logic),
never assert a defect with certainty. That's why this is a separate module rather than
another content_repair_pipeline.py rule: those rules act unilaterally (drop/rename),
this one only ever proposes a candidate for a capped, optional LLM retry - the decision
to trust the flag stays with whatever calls this.

Two smells, each scoped narrowly to avoid the exact failure mode repair_pipeline.py's
2.6 interface-derivation heuristic had (guessing intent from a name/keyword match):
neither rule here guesses WHICH field a method "should" reference - it only flags a
body that references NO fields belonging to its own class at all, which needs no
semantic guess to detect.
"""
import re
from typing import Dict, List, NamedTuple
from src.schemas.java_ast import JavaClass, JavaMethod

DATE_LITERAL = re.compile(r'"\d{4}-\d{1,2}-\d{1,2}"|"\d{1,2}/\d{1,2}/\d{4}"')
DATE_FIELD_TYPES = {"LocalDate", "LocalDateTime", "Date", "Instant"}
TRIVIAL_BOOLEAN_BODY = re.compile(r"^\s*return\s+(true|false)\s*;\s*$")


class ContentSmell(NamedTuple):
    class_name: str
    method: JavaMethod
    reason: str


def _references_any_own_field(body: str, fields: List) -> bool:
    for f in fields:
        if re.search(rf'\b{re.escape(f.name)}\b', body):
            return True
    return False


def find_content_quality_smells(ast_classes: List[JavaClass]) -> Dict[str, List[ContentSmell]]:
    """Pure AST + text-pattern scan, no LLM. Returns smells grouped by class name."""
    smells: Dict[str, List[ContentSmell]] = {}

    for cls in ast_classes:
        found: List[ContentSmell] = []

        for m in cls.methods:
            if not m.body:
                continue

            # Smell A: boolean method whose ENTIRE body is a bare `return true;`/
            # `return false;` literal - not "returns a constant sometimes" (that can
            # be legitimate), the whole body is nothing but the constant, and it
            # references zero of the class's own fields. A method with real domain
            # logic behind a boolean result almost always touches at least one field.
            if (
                m.return_type
                and m.return_type.name == "boolean"
                and TRIVIAL_BOOLEAN_BODY.match(m.body)
                and not _references_any_own_field(m.body, cls.fields)
                and cls.fields
            ):
                found.append(ContentSmell(cls.name, m, "boolean method body is a bare literal, references no fields"))

            # Smell B: a literal date/time string embedded in a method body, on a
            # class that has its own date-typed field the body never references -
            # a live-observed pattern (checkoutItem() hardcoding "2023-12-31" instead
            # of using the class's own due-date field or LocalDate.now()).
            date_fields = [f for f in cls.fields if f.type_ref.name in DATE_FIELD_TYPES]
            if date_fields and DATE_LITERAL.search(m.body) and not _references_any_own_field(m.body, date_fields):
                found.append(ContentSmell(cls.name, m, "hardcoded date literal in body, ignores the class's own date field"))

        if found:
            smells[cls.name] = found

    return smells
