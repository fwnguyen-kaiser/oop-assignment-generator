import os
import shutil
import tempfile
from typing import Optional, Tuple
from src.schemas.logical_plan import SketchPlan
from src.pipeline import compile_logical_plan
from src.builders.java_builder import JavaBuilder
from src.validator.compile_gate import is_javac_available, _compile


class SkeletonGate:
    """Phase 2.5 - structural end gate, run once right after Phase 2 (repair_pipeline)
    and before Phase 5a-i spends any LLM tokens writing fields/methods.

    Deliberately reuses the REAL production path - compile_logical_plan ->
    JavaBuilder.build_ast -> render_class -> javac - rather than a second
    "skeleton-only" renderer. A second renderer can silently diverge from the one that
    actually ships: this is exactly how the interface-extends-interface bug went
    unnoticed (compile_logical_plan routed an inheritance edge between two interfaces
    into `implements`, and JavaBuilder never rendered `extends` for an interface at
    all) - nothing exercised the real render path against a pure-structural graph
    until a live javac run caught it. At the point this gate runs, build_ast's output
    already IS the skeleton (relationship fields only, no business fields/methods yet -
    those don't exist until Phase 5a-i), so there is no separate skeleton to construct.
    """

    def __init__(self):
        self.java_builder = JavaBuilder()

    def check(self, repaired_sketch: SketchPlan) -> Tuple[Optional[bool], str]:
        """True = compiles, False = confirmed does NOT compile, None = never checked.

        None used to be reported as True ("javac not found on PATH" returned a pass), which
        is the same conflation peer review already had fixed at Phase 6: "we did not look" is
        not the claim "we looked and it is fine". A caller that cannot tell them apart cannot
        report honestly, and this gate's verdict now feeds output/conformance_report.json.
        """
        if not is_javac_available():
            return None, "javac not found on PATH - skeleton gate skipped."

        plan = compile_logical_plan(repaired_sketch, decisions=["skeleton gate probe - not used for output"])
        ast_classes = self.java_builder.build_ast(plan)
        if not ast_classes:
            return True, ""
        class_map = {c.name: c for c in ast_classes}

        scratch_dir = tempfile.mkdtemp(prefix="skeleton_gate_")
        try:
            for cls in ast_classes:
                code = self.java_builder.render_class(cls, class_map)
                with open(os.path.join(scratch_dir, f"{cls.name}.java"), "w", encoding="utf-8") as f:
                    f.write(code)
            return _compile(scratch_dir)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
