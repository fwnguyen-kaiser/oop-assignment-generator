from pydantic import BaseModel
from typing import List, Dict, Optional

class RelationshipHints(BaseModel):
    inheritance: Optional[List[str]] = None
    composition: Optional[List[str]] = None
    implements: Optional[List[str]] = None
    aggregation: Optional[List[str]] = None
    association: Optional[List[str]] = None

class EntityHints(BaseModel):
    core: List[str]
    optional: Optional[List[str]] = None

class DomainConfig(BaseModel):
    name: str
    description: str
    keywords: List[str]
    entity_hints: Optional[EntityHints] = None
    relationship_hints: Optional[RelationshipHints] = None
    style: str
