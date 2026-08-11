import os
from typing import List
from src.schemas.domain import DomainConfig
from src.schemas.blueprint import BlueprintPreset
from src.schemas.java_ast import JavaClass

class MarkdownBuilder:
    def __init__(self):
        pass
        
    def build_assignment(self, domain: DomainConfig, preset: BlueprintPreset, ast_classes: List[JavaClass], mmd: str, skeletons: dict, design_decisions: List[str] = None, has_composition: bool = None, has_aggregation: bool = None) -> str:
        lines = []
        lines.append(f"# Object-Oriented Programming Assignment: {domain.name}")
        lines.append("")
        lines.append(domain.description)
        lines.append("")

        lines.append("## 1. Class Diagram")
        lines.append("Please implement the system exactly as shown in this diagram:")
        lines.append("")
        lines.append("```mermaid")
        lines.append(mmd)
        lines.append("```")
        lines.append("")

        # Constraint claims are derived from what was ACTUALLY delivered, not just from the preset
        # toggle - a toggle being on does not guarantee the feature made it into the final design
        # (e.g. Phase 1c can fail to produce a genuine interface), and this text must never
        # promise students something the diagram/code doesn't actually contain.
        has_inheritance = any(c.extends for c in ast_classes)
        has_abstract = any(c.is_abstract for c in ast_classes)
        has_interface = any(c.is_interface for c in ast_classes)

        # Composition and aggregation render as IDENTICAL JavaField shapes once compiled down to
        # ast_classes (both are just a private collection field) - that distinction only survives
        # at the LogicalPlan level (composes_with vs aggregates_with). Callers that have access to
        # the LogicalPlan should pass has_composition/has_aggregation explicitly for an accurate
        # claim; otherwise fall back to this coarser heuristic (which cannot tell them apart and
        # will over-claim "composition" for what might actually be aggregation-only).
        if has_composition is None:
            has_composition = any(
                f.type_ref.is_collection and f.type_ref.name in {c.name for c in ast_classes}
                for c in ast_classes for f in c.fields
            )
        if has_aggregation is None:
            has_aggregation = False

        lines.append("## 2. Constraints")
        lines.append(f"- Min Classes: {preset.structure.classes.min}")
        lines.append(f"- Max Classes: {preset.structure.classes.max}")
        if has_inheritance and preset.oop.inheritance and preset.oop.inheritance.enabled:
            lines.append(f"- Inheritance Depth: {preset.oop.inheritance.max_depth}")
        if has_abstract:
            lines.append("- Must include at least one abstract class")
        if has_interface:
            lines.append("- Must include at least one interface")
        if has_composition:
            lines.append("- Must use composition between classes")
        if has_aggregation:
            lines.append("- Must use aggregation between classes")
        lines.append("")

        if design_decisions:
            lines.append("## 3. Design Decisions")
            for decision in design_decisions:
                lines.append(f"- {decision}")
            lines.append("")

        lines.append("## 4. Student Skeletons")
        lines.append("Use the following skeleton code as a starting point. Fill in the missing implementations:")
        lines.append("")

        for name, code in skeletons.items():
            lines.append(f"### {name}.java")
            lines.append("```java")
            lines.append(code)
            lines.append("```")
            lines.append("")

        return "\n".join(lines)
