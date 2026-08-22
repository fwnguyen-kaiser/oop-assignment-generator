"""Regression net for the three closure/observability gaps closed after the epistemic audit:

1. Tier-1's category vocabulary has no completeness proof and cannot have one (javac's
   message space is not ours to bound) - so what is made finite instead is OBSERVATION:
   every unrecognised error shape is counted and surfaced. See TestNovelShapeDetector.
2. The student skeleton - the artifact the student actually receives - was never held to
   the javac oracle the reference solution was held to. See TestCompileSources.
3. Preset requirements were reported by printing a WARNING to stdout that no caller read.
   See TestConformanceReport.

Plus the declaration that makes any preset-boundary claim finite at all: the supported
(domain, preset) matrix must not silently drift from what ships on disk.
"""
import glob
import os

import pytest

from src.schemas.blueprint import (
    BlueprintPreset, ClassCountConfig, FeatureToggle, InheritanceConfig, OopConfig, StructureConfig,
)
from src.schemas.logical_plan import SketchEntity, SketchPlan, SketchRelationship
from src.pipeline import build_conformance_report, max_extends_edges
from src.supported import SUPPORTED_DOMAINS, SUPPORTED_PRESETS, is_supported, supported_matrix
from src.validator import compile_gate
from src.validator.compile_gate import (
    TIER1_KINDS, CompileVerificationGate, compile_sources, novel_shape_signature, parse_errors,
)


def _preset(min_c=1, max_c=10, depth=3, abstraction=True, interface=True, composition=True, aggregation=False):
    return BlueprintPreset(
        difficulty="test",
        structure=StructureConfig(classes=ClassCountConfig(min=min_c, max=max_c)),
        oop=OopConfig(
            inheritance=InheritanceConfig(enabled=True, max_depth=depth),
            abstraction=FeatureToggle(enabled=abstraction),
            interface=FeatureToggle(enabled=interface),
            composition=FeatureToggle(enabled=composition),
            aggregation=FeatureToggle(enabled=aggregation),
        ),
    )


def _entity(name, **kw):
    return SketchEntity(name=name, kind=kw.pop("kind", "core"), note="n", **kw)


def _sketch(entities, relationships=()):
    return SketchPlan(
        design_rationale="r",
        entities=list(entities),
        relationships=[SketchRelationship(from_entity=a, to_entity=b, type=t) for a, b, t in relationships],
    )


class TestNovelShapeDetector:
    def test_tier1_vocabulary_is_exactly_the_five_audited_categories(self):
        """The count lives in code, not in prose, so the README cannot drift away from
        what _apply_tier1_fix actually handles without this failing."""
        assert TIER1_KINDS == (
            "private_access",
            "missing_override",
            "duplicate_method",
            "invalid_override",
            "missing_symbol_class",
        )

    def test_unmatched_javac_message_is_classified_unknown(self):
        stderr = "Order.java:14: error: incompatible types: Widget cannot be converted to Gadget"
        errors = parse_errors(stderr)
        assert [e["kind"] for e in errors] == ["unknown"]
        assert errors[0]["source_class"] == "Order"

    def test_signature_normalises_line_numbers_so_one_shape_counts_once(self):
        a = "Order.java:14: error: unusual thing about 7 items"
        b = "Order.java:99: error: unusual thing about 7 items"
        assert novel_shape_signature(a) == novel_shape_signature(b)
        assert novel_shape_signature(a) == "unusual thing about N items"

    def test_gate_records_and_dedupes_novel_shapes_but_ignores_known_ones(self):
        gate = CompileVerificationGate(java_builder=None)
        blocks = [
            "Order.java:14: error: some brand new shape",
            "Order.java:81: error: some brand new shape",
            "Order.java:3: error: method pay() is already defined in class Order",
        ]
        gate._record_novel_shapes(parse_errors(os.linesep.join(blocks)))

        assert len(gate.novel_error_shapes) == 1, gate.novel_error_shapes
        assert gate.novel_error_shapes[0]["signature"] == "some brand new shape"
        assert any("NOVEL javac shape" in line for line in gate.report)

    def test_a_fully_recognised_error_set_records_nothing(self):
        gate = CompileVerificationGate(java_builder=None)
        gate._record_novel_shapes(parse_errors(
            "Order.java:3: error: method pay() is already defined in class Order"
        ))
        assert gate.novel_error_shapes == []


class TestCompileSources:
    def test_returns_none_when_javac_is_unavailable(self, monkeypatch):
        """None is not False and not True: 'we did not look' is a third state, the same
        distinction Phase 6's success flag already makes."""
        monkeypatch.setattr(compile_gate, "is_javac_available", lambda: False)
        ok, stderr = compile_sources({"A": "public class A {}"})
        assert ok is None and stderr == ""

    @pytest.mark.skipif(not compile_gate.is_javac_available(), reason="javac not on PATH")
    def test_compiles_valid_sources(self):
        ok, _ = compile_sources({"A": "public class A {}", "B": "public class B extends A {}"})
        assert ok is True

    @pytest.mark.skipif(not compile_gate.is_javac_available(), reason="javac not on PATH")
    def test_rejects_a_non_void_method_with_no_return(self):
        """The exact shape body-stubbing would produce if it ever forgot a return value -
        which is why the skeleton needs its own gate rather than inheriting the solution's."""
        ok, stderr = compile_sources({"A": "public class A { public int f() { } }"})
        assert ok is False
        assert "return" in stderr


class TestMaxExtendsEdges:
    def test_counts_edges_not_nodes(self):
        s = _sketch([_entity("A"), _entity("B", extends="A"), _entity("C", extends="B")])
        assert max_extends_edges(s) == 2

    def test_lone_class_is_zero(self):
        assert max_extends_edges(_sketch([_entity("A")])) == 0

    def test_terminates_on_a_cycle(self):
        s = _sketch([_entity("A", extends="B"), _entity("B", extends="A")])
        assert max_extends_edges(s) >= 1


class TestConformanceReport:
    def test_all_satisfied_on_a_conforming_design(self):
        s = _sketch(
            [_entity("Base", is_abstract=True), _entity("Impl", extends="Base"), _entity("Payable", is_interface=True)],
            [("Impl", "Payable", "implements"), ("Base", "Impl", "composition")],
        )
        report = build_conformance_report(s, _preset(), skeleton_gate_ok=True)
        assert report["all_satisfied"] is True, report["unsatisfied"]
        assert report["entity_count"] == 3

    def test_required_interface_missing_is_reported_unsatisfied(self):
        s = _sketch([_entity("A"), _entity("B", extends="A")], [("A", "B", "composition")])
        report = build_conformance_report(s, _preset(abstraction=False), skeleton_gate_ok=True)
        assert report["all_satisfied"] is False
        assert "interface" in report["unsatisfied"]

    def test_disabled_toggle_is_never_a_failure(self):
        s = _sketch([_entity("A")])
        report = build_conformance_report(
            s, _preset(abstraction=False, interface=False, composition=False), skeleton_gate_ok=True
        )
        assert "interface" not in report["unsatisfied"]
        assert "composition" not in report["unsatisfied"]

    def test_max_classes_breach_is_reported(self):
        s = _sketch([_entity("E" + str(i)) for i in range(5)])
        preset = _preset(max_c=3, abstraction=False, interface=False, composition=False)
        assert "max_classes" in build_conformance_report(s, preset, skeleton_gate_ok=True)["unsatisfied"]

    def test_depth_breach_is_reported_against_the_rule_that_enforces_it(self):
        s = _sketch([_entity("A"), _entity("B", extends="A"), _entity("C", extends="B")])
        preset = _preset(depth=1, abstraction=False, interface=False, composition=False)
        assert "max_extends_edges" in build_conformance_report(s, preset, skeleton_gate_ok=True)["unsatisfied"]

    def test_unchecked_skeleton_gate_is_not_counted_as_a_failure(self):
        # Needs a real extends edge: inheritance is enabled in _preset, so a lone class
        # would fail that requirement and mask what this test is actually about.
        s = _sketch([_entity("A"), _entity("B", extends="A")])
        preset = _preset(abstraction=False, interface=False, composition=False)
        assert build_conformance_report(s, preset, skeleton_gate_ok=None)["all_satisfied"] is True
        assert build_conformance_report(s, preset, skeleton_gate_ok=False)["all_satisfied"] is False


class TestSkeletonGateTriState:
    def test_check_returns_none_when_javac_missing(self, monkeypatch):
        from src.validator import skeleton_gate as sg
        monkeypatch.setattr(sg, "is_javac_available", lambda: False)
        ok, msg = sg.SkeletonGate().check(_sketch([_entity("A")]))
        assert ok is None
        assert "javac not found" in msg


class TestSupportedMatrix:
    def test_declaration_matches_what_actually_ships(self):
        """If a domain or preset is added or removed on disk without updating the
        declaration, every 'verified over 15 combinations' claim silently becomes false."""
        on_disk_domains = {p.replace(os.sep, "/") for p in glob.glob("configs/domains/*.yaml")}
        on_disk_presets = {p.replace(os.sep, "/") for p in glob.glob("configs/presets/*.yaml")}
        assert on_disk_domains == set(SUPPORTED_DOMAINS)
        assert on_disk_presets == set(SUPPORTED_PRESETS)

    def test_matrix_is_the_full_cross_product(self):
        assert len(supported_matrix()) == len(SUPPORTED_DOMAINS) * len(SUPPORTED_PRESETS) == 15

    def test_membership_is_path_shape_agnostic(self):
        assert is_supported("configs/domains/library.yaml", "configs/presets/advanced.yaml")
        assert is_supported(os.path.join("configs", "domains", "library.yaml"), "configs/presets/advanced.yaml")
        assert not is_supported("configs/domains/does_not_exist.yaml", "configs/presets/advanced.yaml")
