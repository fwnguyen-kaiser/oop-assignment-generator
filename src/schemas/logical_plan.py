import re
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Literal

class SemanticEntity(BaseModel):
    name: str
    description: Optional[str] = None
    is_abstract: bool = False
    is_interface: bool = False
    inherits_from: Optional[str] = None
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

    @field_validator('name')
    @classmethod
    def must_be_pascal_case(cls, v: str) -> str:
        if not re.match(r'^[A-Z][a-zA-Z0-9]*$', v):
            raise ValueError(f"Entity name '{v}' must be PascalCase and a valid Java identifier.")
        return v

class SketchRelationship(BaseModel):
    from_entity: str = Field(description="The source entity name")
    to_entity: str = Field(description="The target entity name")
    type: Literal["inheritance", "composition", "aggregation", "association", "implements"] = Field(description="The OOP relationship type")

class SketchPlan(BaseModel):
    design_rationale: str = Field(description="Brief explanation of the domain design")
    entities: List[SketchEntity]
    relationships: List[SketchRelationship]
