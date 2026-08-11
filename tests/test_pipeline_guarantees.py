from unittest.mock import MagicMock
from src.schemas.logical_plan import SketchPlan, SketchEntity, SketchRelationship
from src.schemas.domain import DomainConfig
from src.pipeline import apply_min_classes_guarantee, apply_interface_guarantee


def _sketch(entities, relationships=None):
    return SketchPlan(
        design_rationale="test",
        entities=[SketchEntity(name=n, kind="core", note="") for n in entities],
        relationships=[SketchRelationship(from_entity=f, to_entity=t, type=ty) for f, t, ty in (relationships or [])],
    )


def _domain():
    return DomainConfig(name="test", description="test", keywords=[], style="formal")


class TestMinClassesGuarantee:
    def test_no_call_when_already_at_min(self):
        llm = MagicMock()
        repair_engine = MagicMock()
        sketch = _sketch(["A", "B", "C"])
        result = apply_min_classes_guarantee(sketch, llm, repair_engine, _domain(), min_classes=3)
        assert result is sketch
        llm.generate_missing_entities.assert_not_called()

    def test_stops_as_soon_as_min_is_reached(self):
        llm = MagicMock()
        llm.generate_missing_entities.return_value = _sketch(["D"])
        repair_engine = MagicMock()
        # repair() just returns whatever it's given (no-op passthrough for this test)
        repair_engine.repair.side_effect = lambda s: s

        sketch = _sketch(["A", "B"])
        result = apply_min_classes_guarantee(sketch, llm, repair_engine, _domain(), min_classes=3, max_attempts=3)

        assert len(result.entities) == 3
        assert llm.generate_missing_entities.call_count == 1

    def test_capped_at_max_attempts_when_llm_keeps_failing(self):
        llm = MagicMock()
        llm.generate_missing_entities.side_effect = Exception("API down")
        repair_engine = MagicMock()

        sketch = _sketch(["A"])
        result = apply_min_classes_guarantee(sketch, llm, repair_engine, _domain(), min_classes=5, max_attempts=3)

        assert llm.generate_missing_entities.call_count == 3
        assert len(result.entities) == 1

    def test_repair_shrinking_entities_triggers_another_attempt(self):
        """If repair() drops entities again (e.g. 2.5/2.8 trigger on the new ones), the loop
        must recompute missing_count fresh each round rather than trusting a stale count."""
        llm = MagicMock()
        llm.generate_missing_entities.side_effect = [_sketch(["D", "E"]), _sketch(["F"])]
        repair_engine = MagicMock()
        # First repair() call drops one of the two newly added entities (simulates 2.5/2.8)
        repair_engine.repair.side_effect = [_sketch(["A", "D"]), _sketch(["A", "D", "F"])]

        sketch = _sketch(["A"])
        result = apply_min_classes_guarantee(sketch, llm, repair_engine, _domain(), min_classes=3, max_attempts=3)

        assert llm.generate_missing_entities.call_count == 2
        assert len(result.entities) == 3


class TestInterfaceGuarantee:
    def test_no_call_when_not_required(self):
        llm = MagicMock()
        repair_engine = MagicMock()
        sketch = _sketch(["A"])
        result = apply_interface_guarantee(sketch, llm, repair_engine, _domain(), interface_required=False)
        assert result is sketch
        llm.generate_missing_interface.assert_not_called()

    def test_no_call_when_interface_already_exists(self):
        llm = MagicMock()
        repair_engine = MagicMock()
        entities = [SketchEntity(name="A", kind="core", note=""), SketchEntity(name="B", kind="supporting", note="", is_interface=True)]
        sketch = SketchPlan(design_rationale="test", entities=entities, relationships=[])
        result = apply_interface_guarantee(sketch, llm, repair_engine, _domain(), interface_required=True)
        assert result is sketch
        llm.generate_missing_interface.assert_not_called()

    def test_stops_as_soon_as_interface_appears(self):
        llm = MagicMock()
        interface_entities = [SketchEntity(name="TaxCalculatable", kind="supporting", note="", is_interface=True)]
        llm.generate_missing_interface.return_value = SketchPlan(design_rationale="test", entities=interface_entities, relationships=[])
        repair_engine = MagicMock()
        repair_engine.repair.side_effect = lambda s: s

        sketch = _sketch(["A"])
        result = apply_interface_guarantee(sketch, llm, repair_engine, _domain(), interface_required=True, max_attempts=2)

        assert any(e.is_interface for e in result.entities)
        assert llm.generate_missing_interface.call_count == 1

    def test_capped_at_max_attempts_and_ships_without_interface(self):
        """Reproduces the real 2.11-orphan-demotion case: the LLM proposes something but repair()
        demotes it back to non-interface every time (e.g. no genuine implementer). Must stop at
        max_attempts, not loop forever, and must not crash - it ships without an interface."""
        llm = MagicMock()
        orphan_entities = [SketchEntity(name="PaymentGateway", kind="supporting", note="", is_interface=True)]
        llm.generate_missing_interface.return_value = SketchPlan(design_rationale="test", entities=orphan_entities, relationships=[])
        repair_engine = MagicMock()
        # repair() always demotes the orphan interface back to a concrete class (mirrors real rule 2.11)
        def fake_repair(s):
            for e in s.entities:
                e.is_interface = False
            return s
        repair_engine.repair.side_effect = fake_repair

        sketch = _sketch(["A"])
        result = apply_interface_guarantee(sketch, llm, repair_engine, _domain(), interface_required=True, max_attempts=2)

        assert llm.generate_missing_interface.call_count == 2
        assert not any(e.is_interface for e in result.entities)

    def test_llm_exception_does_not_crash_and_exhausts_attempts(self):
        llm = MagicMock()
        llm.generate_missing_interface.side_effect = Exception("API down")
        repair_engine = MagicMock()

        sketch = _sketch(["A"])
        result = apply_interface_guarantee(sketch, llm, repair_engine, _domain(), interface_required=True, max_attempts=2)

        assert llm.generate_missing_interface.call_count == 2
        assert result is sketch
