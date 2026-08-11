from pydantic import BaseModel, Field
from typing import Optional

class ClassCountConfig(BaseModel):
    min: int = Field(default=3, ge=1)
    max: int = Field(default=5, ge=1)

class FeatureToggle(BaseModel):
    enabled: bool

class InheritanceConfig(BaseModel):
    enabled: bool
    max_depth: int = Field(default=1, ge=1)
    
class StructureConfig(BaseModel):
    classes: ClassCountConfig
    
class OopConfig(BaseModel):
    inheritance: Optional[InheritanceConfig] = None
    abstraction: Optional[FeatureToggle] = None
    interface: Optional[FeatureToggle] = None
    composition: Optional[FeatureToggle] = None
    aggregation: Optional[FeatureToggle] = None

class BlueprintPreset(BaseModel):
    difficulty: str
    structure: StructureConfig
    oop: OopConfig
