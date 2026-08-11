from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship
from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.validator.repair_pipeline import StructuralRepairPipeline

def make_sketch(entities_names, edges):
    entities = []
    for name in entities_names:
        entities.append(SketchEntity(
            name=name,
            kind="supporting" if name == "InterestBearing" else "core",
            note="Test entity"
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
    """Node chỉ nhận implements-edges, chưa từng là true-parent qua inheritance"""
    sketch = make_sketch(
        ["SavingsAccount", "CheckingAccount", "Account", "InterestBearing"],
        [("SavingsAccount", "Account", "inheritance"),
         ("SavingsAccount", "InterestBearing", "inheritance"),  # sẽ bị 2.1 downgrade thành implements
         ("CheckingAccount", "Account", "inheritance"),
         ("CheckingAccount", "InterestBearing", "inheritance")]
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
