import re
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Literal

class SemanticEntity(BaseModel):
    name: str
    description: Optional[str] = None
    is_abstract: bool = False
    is_interface: bool = False
    inherits_from: Optional[str] = None
    # Only populated when this entity IS an interface: JLS allows an interface to
    # extend multiple interfaces (unlike a class, which has at most one superclass -
    # see `inherits_from` above). Kept as a separate field rather than overloading
    # `inherits_from` so JavaBuilder can tell "single-parent class extends" apart
    # from "multi-parent interface extends" without re-deriving it from is_interface.
    extends_interfaces: Optional[List[str]] = None
    implements: Optional[List[str]] = None
    composes_with: Optional[List[str]] = None
    aggregates_with: Optional[List[str]] = None
    associated_with: Optional[List[str]] = None

class LogicalPlan(BaseModel):
    design_decisions: List[str] = Field(
        description="Explain your design decisions domain-first. Include how you fixed errors if this is a retry."
    )
    domain_entities: List[SemanticEntity]
    support_entities: List[SemanticEntity]

# --- Phase 1: Sketch Schemas ---

class SketchEntity(BaseModel):
    name: str = Field(description="Name of the entity (e.g. Account, Customer)")
    kind: Literal["core", "supporting"] = Field(description="Core domain entity or supporting/auxiliary entity")
    note: str = Field(description="Brief explanation of what this entity does")
    is_abstract: bool = False
    is_interface: bool = False
    # Inheritance lives on the entity itself, not in `relationships`, and is
    # kind-aware by construction: `extends` is a single Optional[str] because a
    # class/abstract class has at most one superclass in Java (JLS SS8.1.4) - the LLM
    # cannot even express two parents for one class, the schema has no second slot.
    # `extends_interfaces` is a list because an interface may extend several
    # interfaces (JLS SS9.1.3) - only meaningful when is_interface=True. This is the
    # same "illegal states unrepresentable" technique already used for Phase 5a-i's
    # signature/body split, applied one phase earlier.
    extends: Optional[str] = Field(
        default=None,
        description="Name of this entity's single superclass, if any. Only used when is_interface=False - target must be a class or abstract class, never an interface.",
    )
    extends_interfaces: List[str] = Field(
        default_factory=list,
        description="Names of interfaces this interface extends. Only used when is_interface=True - an interface may extend multiple other interfaces.",
    )

    @field_validator('name')
    @classmethod
    def must_be_pascal_case(cls, v: str) -> str:
        if not re.match(r'^[A-Z][a-zA-Z0-9]*$', v):
            raise ValueError(f"Entity name '{v}' must be PascalCase and a valid Java identifier.")
        return v

class SketchRelationship(BaseModel):
    from_entity: str = Field(description="The source entity name")
    to_entity: str = Field(description="The target entity name")
    # "inheritance" removed from this Literal on purpose - it now lives on
    # SketchEntity.extends/extends_interfaces above, so the LLM (Gemini's structured
    # output is constrained to this exact JSON schema) can no longer emit a
    # relationship-list inheritance edge at all. repair_pipeline still uses the string
    # "inheritance" internally as a SketchRelationship.type value when it constructs
    # its OWN edges for downstream compile_logical_plan compatibility - that's a plain
    # str field, not re-validated against this Literal, so it's unaffected.
    type: Literal["composition", "aggregation", "association", "implements"] = Field(description="The OOP relationship type")

class SketchPlan(BaseModel):
    design_rationale: str = Field(description="Brief explanation of the domain design")
    entities: List[SketchEntity]
    relationships: List[SketchRelationship]
