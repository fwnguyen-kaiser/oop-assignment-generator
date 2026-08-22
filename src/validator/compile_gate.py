import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Tuple
from src.schemas.java_ast import JavaClass, JavaField, JavaTypeRef
from src.validator.content_repair_pipeline import default_body_for_return_type


def is_javac_available() -> bool:
    return shutil.which("javac") is not None


def _compile(source_dir: str) -> Tuple[bool, str]:
    java_files = [f for f in os.listdir(source_dir) if f.endswith(".java")]
    if not java_files:
        return True, ""
    out_dir = os.path.join(source_dir, "_classes")
    os.makedirs(out_dir, exist_ok=True)
    result = subprocess.run(
        ["javac", "-d", out_dir] + java_files,
        cwd=source_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, result.stderr


def _split_error_blocks(stderr: str) -> List[str]:
    """javac errors start with 'File.java:<line>: error: <msg>' and may span several
    following lines (e.g. 'symbol:', 'location:'). Split into one block per error."""
    lines = stderr.splitlines()
    blocks = []
    current = []
    header = re.compile(r"^.+\.java:\d+: error: .+$")
    for line in lines:
        if header.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


_PRIVATE_ACCESS = re.compile(r"error: (?P<symbol>\w+) has private access in (?P<owner>\w+)")
# javac uses a DIFFERENT verb depending on whether the mismatch is against a
# superclass method ("cannot override") or an interface method ("cannot implement") -
# same error category, two message shapes. Found live: a class implementing an
# interface with a mismatched return type produced "cannot implement", which this
# regex used to miss entirely, falling through as "unknown" and never getting fixed.
_INVALID_OVERRIDE = re.compile(r"error: \w+\(\)? ?.* in (?P<child>\w+) cannot (?:override|implement) \w+\(.*\) in (?P<parent>\w+)")
_INVALID_OVERRIDE_METHOD = re.compile(r"error: (?P<method>\w+)\(")
_DUPLICATE_METHOD = re.compile(r"error: method (?P<method>\w+)\(.*\) is already defined in class (?P<owner>\w+)")
_MISSING_OVERRIDE = re.compile(r"error: (?P<child>\w+) is not abstract and does not override abstract method (?P<method>\w+)\([^)]*\) in (?P<parent>\w+)")
_MISSING_SYMBOL_CLASS = re.compile(r"error: cannot find symbol[\s\S]*?symbol:\s+class (?P<symbol>\w+)")

# Superset of java_builder's own whitelist - covers common stdlib types the AST/LLM layers
# didn't have a permanent fix for yet. Anything not in here can't be resolved deterministically
# (we have no way to guess an unknown/hallucinated type's package) and escalates to Tier 2.
COMMON_TYPE_IMPORTS = {
    "UUID": "java.util.UUID",
    "Optional": "java.util.Optional",
    "Instant": "java.time.Instant",
    "Duration": "java.time.Duration",
    "Period": "java.time.Period",
    "Currency": "java.util.Currency",
    "Random": "java.util.Random",
    "Comparator": "java.util.Comparator",
    "HashMap": "java.util.HashMap",
    "LinkedList": "java.util.LinkedList",
    "TreeMap": "java.util.TreeMap",
    "Objects": "java.util.Objects",
}


_FILE_HEADER = re.compile(r"^(?P<file>[\w$]+)\.java:\d+: error:")


def _source_class(block: str) -> str:
    """Best-effort class name from the file the error was reported against (Class.java -> Class).
    Used as the fallback target for Tier 2 when a block doesn't match a known pattern well
    enough to name the owner/child class directly."""
    m = _FILE_HEADER.match(block)
    return m.group("file") if m else None


def parse_errors(stderr: str) -> List[Dict]:
    found = []
    for block in _split_error_blocks(stderr):
        source_class = _source_class(block)

        m = _PRIVATE_ACCESS.search(block)
        if m:
            found.append({"kind": "private_access", "source_class": source_class, **m.groupdict(), "raw": block})
            continue

        m = _MISSING_OVERRIDE.search(block)
        if m:
            found.append({"kind": "missing_override", "source_class": source_class, **m.groupdict(), "raw": block})
            continue

        m = _DUPLICATE_METHOD.search(block)
        if m:
            found.append({"kind": "duplicate_method", "source_class": source_class, **m.groupdict(), "raw": block})
            continue

        m = _INVALID_OVERRIDE.search(block)
        if m:
            method_m = _INVALID_OVERRIDE_METHOD.search(block)
            found.append({
                "kind": "invalid_override",
                "method": method_m.group("method") if method_m else None,
                "source_class": source_class,
                **m.groupdict(),
                "raw": block,
            })
            continue

        m = _MISSING_SYMBOL_CLASS.search(block)
        if m:
            found.append({"kind": "missing_symbol_class", "source_class": source_class, **m.groupdict(), "raw": block})
            continue

        found.append({"kind": "unknown", "source_class": source_class, "raw": block})
    return found


class CompileVerificationGate:
    """Phase 6 - ultimate safeguard. Renders classes to a scratch dir, runs the real javac,
    and repairs whatever it complains about. Tier 1 (free, deterministic) handles known error
    shapes by reading the real compiler message - not by guessing from source text. Tier 2
    (costly, capped to a single attempt) is the last resort for genuinely novel errors."""

    def __init__(self, java_builder):
        self.java_builder = java_builder
        self.report: List[str] = []

    def _log(self, msg: str):
        self.report.append(msg)
        print(f"[COMPILE_GATE] {msg}")

    def _render_to_scratch(self, ast_classes: List[JavaClass], scratch_dir: str):
        class_map = {c.name: c for c in ast_classes}
        for cls in ast_classes:
            code = self.java_builder.render_class(cls, class_map)
            with open(os.path.join(scratch_dir, f"{cls.name}.java"), "w", encoding="utf-8") as f:
                f.write(code)

    def _apply_tier1_fix(self, error: Dict, class_map: Dict[str, JavaClass]) -> bool:
        """Taxonomy note (audited, not just asserted): of the 5 kinds handled here, only
        `missing_symbol_class` is genuinely undecidable from the AST alone - it needs a
        classpath/symbol table equivalent to what a compiler already provides for free.

        The other 4 (`private_access`, `duplicate_method`, `invalid_override`,
        `missing_override`) are each FULLY decidable via pure AST analysis, and are
        already supposed to be prevented before this ever runs, by their primary owner:
          - private_access     -> content_repair_pipeline.py 5.8 (protected promotion)
          - duplicate_method    -> content_repair_pipeline.py 5.6 (method self-dedupe)
          - invalid_override    -> content_repair_pipeline.py 5.7 (invalid override rename)
          - missing_override    -> content_repair_pipeline.py 5.9 (contract fulfillment)
        If any of these 4 ever reach here, either (a) the Taxonomy A rule above it had a
        bug in isolation (happened twice already this session - bare-identifier and
        inherited-accessor-collision fixes), or (b) more interestingly, two individually
        correct Taxonomy A rules interacted badly - see docs/logical-plan-pipeline-report.md
        4.4's "multi-implements self-cancels" case: rule 2.1 and rule 2.6 were each
        correct on their own, but 2.6 forgot to count an edge type that only exists
        because of 2.1's fix, and no single rule's own unit test could ever catch that
        since it only manifests in the ordered combination of both. This branch is kept
        for that reason: it is a ground-truth cross-validation pass over the COMBINED
        output of every Taxonomy A rule together, which is structurally different from,
        and not replaced by, each rule's own isolated unit tests - not because the check
        itself requires a compiler to be decided in the first place.
        """
        kind = error["kind"]

        if kind == "private_access":  # safety net for 5.8, not compiler-exclusive
            owner = class_map.get(error["owner"])
            if not owner:
                return False
            for f in owner.fields:
                if f.name == error["symbol"] and f.modifier == "private":
                    f.modifier = "protected"
                    self._log(f"private_access: promoted {error['owner']}.{error['symbol']} to protected")
                    return True
            return False

        if kind == "duplicate_method":  # safety net for 5.6, not compiler-exclusive
            owner = class_map.get(error["owner"])
            if not owner:
                return False
            seen = False
            kept = []
            for m in owner.methods:
                if m.name == error["method"]:
                    if seen:
                        continue
                    seen = True
                kept.append(m)
            if len(kept) < len(owner.methods):
                owner.methods = kept
                self._log(f"duplicate_method: dropped extra '{error['method']}' on {error['owner']}")
                return True
            return False

        if kind == "invalid_override":  # safety net for 5.7, not compiler-exclusive
            child = class_map.get(error["child"])
            if not child or not error.get("method"):
                return False
            for m in child.methods:
                if m.name == error["method"]:
                    old_name = m.name
                    m.name = f"{m.name}Impl"
                    self._log(f"invalid_override: renamed {error['child']}.{old_name} -> {m.name}")
                    return True
            return False

        if kind == "missing_override":  # safety net for 5.9, not compiler-exclusive
            child = class_map.get(error["child"])
            parent = class_map.get(error["parent"])
            if not child or not parent:
                return False
            source_method = next((m for m in parent.methods if m.name == error["method"]), None)
            if not source_method:
                return False
            new_method = source_method.model_copy(deep=True)
            new_method.body = default_body_for_return_type(new_method.return_type)
            new_method.is_abstract = False
            child.methods.append(new_method)
            self._log(f"missing_override: added stub {error['child']}.{error['method']}() to satisfy {error['parent']}")
            return True

        if kind == "missing_symbol_class":  # the one genuinely compiler-exclusive case
            symbol = error["symbol"]
            if symbol in COMMON_TYPE_IMPORTS:
                self.java_builder.standard_types[symbol] = COMMON_TYPE_IMPORTS[symbol]
                self._log(f"missing_symbol_class: registered import for '{symbol}' ({COMMON_TYPE_IMPORTS[symbol]})")
                return True
            return False

        return False

    def verify_and_repair(self, ast_classes: List[JavaClass], domain, provider, max_tier1: int = 3, max_tier2: int = 1) -> List[JavaClass]:
        if not is_javac_available():
            self._log("javac not found on PATH - skipping compile verification (Phase 6 disabled for this run).")
            return ast_classes

        class_map = {c.name: c for c in ast_classes}
        scratch_dir = tempfile.mkdtemp(prefix="compile_gate_")

        try:
            success = False
            for attempt in range(max_tier1):
                self._render_to_scratch(ast_classes, scratch_dir)
                success, stderr = _compile(scratch_dir)
                if success:
                    if attempt > 0:
                        self._log(f"Tier 1: compiled successfully after {attempt} deterministic fix round(s).")
                    return ast_classes

                errors = parse_errors(stderr)
                any_fixed = False
                applied_this_round = set()
                for err in errors:
                    dedupe_key = (err["kind"], err.get("owner") or err.get("child") or err.get("symbol"), err.get("symbol") or err.get("method"))
                    if dedupe_key in applied_this_round:
                        continue
                    if self._apply_tier1_fix(err, class_map):
                        any_fixed = True
                        applied_this_round.add(dedupe_key)
                if not any_fixed:
                    break

            if success:
                return ast_classes

            # Tier 2: capped, last-resort LLM repair for whatever Tier 1 couldn't resolve
            self._render_to_scratch(ast_classes, scratch_dir)
            success, stderr = _compile(scratch_dir)
            if success:
                return ast_classes

            errors = parse_errors(stderr)
            affected_classes = {
                e.get("owner") or e.get("child") or e.get("source_class") for e in errors
            } - {None}
            affected_classes &= set(class_map.keys())

            if not affected_classes:
                self._log(f"Tier 1 exhausted, no resolvable class identified. Shipping best-effort output with known compile errors:\n{stderr}")
                return ast_classes

            for attempt in range(max_tier2):
                self._log(f"Tier 2 (attempt {attempt+1}/{max_tier2}): asking LLM to fix {sorted(affected_classes)} given real compiler errors.")
                try:
                    fixes = provider.fix_compile_errors(
                        [class_map[name] for name in affected_classes], stderr, domain.name, domain.description
                    )
                    for fix in fixes:
                        if fix.name in class_map:
                            class_map[fix.name].fields = fix.fields
                            class_map[fix.name].methods = fix.methods
                except Exception as e:
                    self._log(f"Tier 2 LLM call failed: {e}")
                    break

                self._render_to_scratch(ast_classes, scratch_dir)
                success, stderr = _compile(scratch_dir)
                if success:
                    self._log("Tier 2: LLM fix resolved the remaining compile errors.")
                    return ast_classes

            if not success:
                self._log(
                    "Compile verification FAILED after Tier 1 + Tier 2 - shipping best-effort output. "
                    f"Remaining javac errors:\n{stderr}"
                )
            return ast_classes
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
