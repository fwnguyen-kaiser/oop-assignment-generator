from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship
from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.validator.repair_pipeline import StructuralRepairPipeline

def make_sketch(entities_names, edges, extends_map=None):
    extends_map = extends_map or {}
    entities = []
    for name in entities_names:
        entities.append(SketchEntity(
            name=name,
            kind="supporting" if name == "InterestBearing" else "core",
            note="Test entity",
            extends=extends_map.get(name),
        ))

    relationships = []
    for f, t, typ in edges:
        relationships.append(SketchRelationship(
            from_entity=f,
            to_entity=t,
            type=typ
        ))

    return SketchPlan(
        design_rationale="Test",
        entities=entities,
        relationships=relationships
    )

def test_pure_implements_target_becomes_interface():
    """Node chỉ nhận implements-edges, chưa từng là true-parent qua inheritance.

    Previously this relied on repair_pipeline's rule 2.1 downgrading a SECOND
    'inheritance' edge from the same class into 'implements' - that pathway no longer
    exists: SketchEntity.extends is a single Optional[str] (Tầng 0 schema change), so a
    class can no longer represent two superclasses in the first place. A schema-
    constrained LLM would declare its ONE real superclass via `extends` and use
    'implements' directly for anything else, so that's how this sketch is built."""
    sketch = make_sketch(
        ["SavingsAccount", "CheckingAccount", "Account", "InterestBearing"],
        [("SavingsAccount", "InterestBearing", "implements"),
         ("CheckingAccount", "InterestBearing", "implements")],
        extends_map={"SavingsAccount": "Account", "CheckingAccount": "Account"},
    )
    
    # Mock preset and domain
    from pydantic import BaseModel
    class MockPreset(BaseModel):
        class OOP(BaseModel):
            class Interface(BaseModel):
                enabled: bool = True
            class Abstraction(BaseModel):
                enabled: bool = True
            class Inheritance(BaseModel):
                max_depth: int = 5
                enabled: bool = True
            class Composition(BaseModel):
                enabled: bool = True
            class Aggregation(BaseModel):
                enabled: bool = True
            interface: Interface = Interface()
            abstraction: Abstraction = Abstraction()
            inheritance: Inheritance = Inheritance()
            composition: Composition = Composition()
            aggregation: Aggregation = Aggregation()
            
        class Structure(BaseModel):
            class Classes(BaseModel):
                min: int = 2
                max: int = 10
            classes: Classes = Classes()
            
        oop: OOP = OOP()
        structure: Structure = Structure()
        
    preset = MockPreset()
    domain = DomainConfig(name="test", description="test", entities=[], keywords=[], style="formal")
    
    pipeline = StructuralRepairPipeline(preset, domain)
    result = pipeline.repair(sketch)

    interest_bearing = next(e for e in result.entities if e.name == "InterestBearing")
    assert interest_bearing.is_interface is True, "InterestBearing should be marked as interface!"

    impl_edges = [r for r in result.relationships if r.type == "implements" and r.to_entity == "InterestBearing"]
    assert len(impl_edges) == 2, f"Expected 2 implements edges, got {len(impl_edges)}"
    print("[SUCCESS] test_pure_implements_target_becomes_interface passed!")

if __name__ == "__main__":
    test_pure_implements_target_becomes_interface()
