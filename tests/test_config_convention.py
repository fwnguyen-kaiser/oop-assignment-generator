"""Encodes the config convention as checks, so it is enforced rather than merely written down.

The prompt for this file was a real bug: `configs/presets/advanced.yaml` shipped
`aggregation: enabled: false` while all five domains hint aggregation, so every advanced run
was guaranteed to fire rule `2.7_aggregation_fallback` and retype those edges. A comment
would not have caught it; nothing was checking that the config files agreed with each other.

Four conventions are locked here:
  1. Every preset declares all five OOP feature keys explicitly (an absent key and
     `enabled: false` behave identically in code, which made the progression unreadable).
  2. beginner -> intermediate -> advanced is monotonically non-decreasing on every dimension.
  3. Domain hints and entity hints reference each other in BOTH directions.
  4. A preset may only disable aggregation where the retype it causes is intended.
"""
import io
import itertools

import pytest
import yaml

from src.schemas.blueprint import BlueprintPreset
from src.schemas.domain import DomainConfig
from src.schemas.logical_plan import SketchEntity, SketchPlan, SketchRelationship
from src.supported import SUPPORTED_DOMAINS, SUPPORTED_PRESETS
from src.validator.compile_gate import is_javac_available
from src.validator.repair_pipeline import StructuralRepairPipeline
from src.validator.skeleton_gate import SkeletonGate

FEATURES = ("inheritance", "abstraction", "interface", "composition", "aggregation")
REL_FIELDS = ("inheritance", "composition", "aggregation", "association", "implements")
# Ordered by difficulty on purpose - test_progression_is_monotonic walks adjacent pairs.
PRESET_ORDER = ("beginner", "intermediate", "advanced")


def _raw(path):
    return yaml.safe_load(io.open(path, encoding="utf-8"))


def _preset(name):
    return BlueprintPreset(**_raw(f"configs/presets/{name}.yaml"))


def _domains():
    return [DomainConfig(**_raw(p)) for p in SUPPORTED_DOMAINS]


def _pairs(domain, field):
    rh = domain.relationship_hints
    return [tuple(x.strip() for x in p.split("->")) for p in ((getattr(rh, field) or []) if rh else [])]


def _hint_names(domain):
    return set(domain.entity_hints.core) | set(domain.entity_hints.optional or [])


def _names_used_in_relationships(domain):
    used = set()
    for field in REL_FIELDS:
        for pair in _pairs(domain, field):
            used |= set(pair)
    return used


def sketch_from_hints(domain: DomainConfig) -> SketchPlan:
    """The sketch a perfectly obedient LLM would produce from this domain's hints.

    The hint set IS a graph, so config-vs-preset contradictions are checkable with no API
    call at all. It deliberately includes every hinted entity, which overshoots the tighter
    presets' max_classes - the real Phase 1 prompt tells the LLM to pick at most that many,
    so excess-class dropping here is an artifact of this fixture and is not asserted on.
    """
    ifaces = {target for _, target in _pairs(domain, "implements")}
    # `A -> B` under `inheritance` reads "A is the parent of B" (e.g. Product -> PhysicalProduct).
    parent_of = {child: parent for parent, child in _pairs(domain, "inheritance")}

    entities = [
        SketchEntity(
            name=name,
            kind=("core" if name in domain.entity_hints.core else "supporting"),
            note="from relationship hints",
            is_interface=(name in ifaces),
            extends=(parent_of.get(name) if name not in ifaces else None),
        )
        for name in list(domain.entity_hints.core) + list(domain.entity_hints.optional or [])
    ]
    rels = [
        SketchRelationship(from_entity=a, to_entity=b, type=field)
        for field in ("composition", "aggregation", "association", "implements")
        for a, b in _pairs(domain, field)
    ]
    return SketchPlan(design_rationale="hint graph", entities=entities, relationships=rels)


class TestPresetConvention:
    @pytest.mark.parametrize("name", PRESET_ORDER)
    def test_every_feature_key_is_declared_explicitly(self, name):
        """An absent key and `enabled: false` are identical to the code, so writing only one
        of them made the three presets impossible to read side by side as a progression.
        Checked against raw YAML, since the pydantic model fills absent keys with None."""
        oop = _raw(f"configs/presets/{name}.yaml")["oop"]
        missing = [f for f in FEATURES if f not in oop]
        assert not missing, f"{name}.yaml does not declare: {missing}"
        for feature in FEATURES:
            assert "enabled" in oop[feature], f"{name}.yaml: {feature} has no `enabled` key"

    def test_shipped_preset_names_match_the_declared_order(self):
        declared = {p.rsplit("/", 1)[1][: -len(".yaml")] for p in SUPPORTED_PRESETS}
        assert declared == set(PRESET_ORDER)

    @pytest.mark.parametrize("lower,higher", list(zip(PRESET_ORDER, PRESET_ORDER[1:])))
    def test_progression_is_monotonic(self, lower, higher):
        """Aggregation used to go true at intermediate and back to false at advanced - the
        only non-monotonic dimension, which is what identified it as an oversight rather than
        a pedagogical choice."""
        lo, hi = _preset(lower), _preset(higher)
        assert hi.structure.classes.min >= lo.structure.classes.min
        assert hi.structure.classes.max >= lo.structure.classes.max
        assert hi.oop.inheritance.max_depth >= lo.oop.inheritance.max_depth
        for feature in FEATURES:
            lo_on = getattr(lo.oop, feature).enabled
            hi_on = getattr(hi.oop, feature).enabled
            assert hi_on >= lo_on, (
                f"{feature} goes {lo_on} at {lower} -> {hi_on} at {higher}; the progression "
                "must be monotonically non-decreasing"
            )

    def test_only_beginner_may_disable_aggregation(self):
        """Disabling aggregation is PROHIBITIVE, not permissive: rule 2.7_aggregation_fallback
        actively retypes those edges to composition. Every shipped domain hints aggregation, so
        any preset that disables it silently rewrites the domain's own stated semantics. That is
        acceptable exactly once - at beginner, where the distinction is not taught and both
        forms render to identical Java anyway - and is a bug anywhere else."""
        disabled = [n for n in PRESET_ORDER if not _preset(n).oop.aggregation.enabled]
        assert disabled in ([], ["beginner"]), (
            f"presets disabling aggregation: {disabled} - only beginner may, see this test's docstring"
        )


class TestDomainConvention:
    @pytest.mark.parametrize("path", SUPPORTED_DOMAINS)
    def test_loads_and_stays_inside_the_measured_hint_envelope(self, path):
        DomainConfig(**_raw(path))  # raises if entity_hints exceeds MAX_ENTITY_HINTS

    @pytest.mark.parametrize("path", SUPPORTED_DOMAINS)
    def test_every_relationship_hint_name_is_a_declared_entity(self, path):
        """animal.yaml and banking.yaml used to name entities in relationship_hints
        (BodyPart, Bank, BankAccount, Mammal...) that appeared nowhere in entity_hints, so the
        prompt asked for relationships between things it never offered as candidates."""
        domain = DomainConfig(**_raw(path))
        undeclared = _names_used_in_relationships(domain) - _hint_names(domain)
        assert not undeclared, f"{path}: used in relationship_hints but not declared: {sorted(undeclared)}"

    @pytest.mark.parametrize("path", SUPPORTED_DOMAINS)
    def test_every_declared_entity_appears_in_some_relationship_hint(self, path):
        """The other direction: an entity candidate with no hinted relationship gives the LLM
        no guidance on how it connects, and if it gets picked anyway repair has to invent an
        edge for it (rule 2.8). Librarian and Spell were the two loose ends."""
        domain = DomainConfig(**_raw(path))
        orphans = _hint_names(domain) - _names_used_in_relationships(domain)
        assert not orphans, f"{path}: declared but unused in relationship_hints: {sorted(orphans)}"

    @pytest.mark.parametrize("path", SUPPORTED_DOMAINS)
    def test_relationship_hints_are_one_pair_per_line(self, path):
        """`Animal -> Mammal -> Dog` was a three-node chain in a single string, unparseable as
        a pair and inconsistent with every other domain."""
        domain = DomainConfig(**_raw(path))
        rh = domain.relationship_hints
        for field in REL_FIELDS:
            for hint in ((getattr(rh, field) or []) if rh else []):
                assert hint.count("->") == 1, f"{path}: {field} hint is not a single pair: {hint!r}"


MATRIX = list(itertools.product(SUPPORTED_DOMAINS, PRESET_ORDER))


class TestMatrixCoherence:
    """Runs the hint graph through Phase 2 for all 15 declared combinations. No API calls."""

    @pytest.mark.parametrize("domain_path,preset_name", MATRIX)
    def test_retype_happens_exactly_where_the_preset_intends_it(self, domain_path, preset_name):
        """The advanced-aggregation bug generalised: a retype is legitimate only when the
        preset deliberately forbids a relationship type the domain hints. Anywhere else it
        means two config files disagree - which is precisely what shipped unnoticed."""
        domain = DomainConfig(**_raw(domain_path))
        preset = _preset(preset_name)
        pipeline = StructuralRepairPipeline(preset, domain)
        pipeline.repair(sketch_from_hints(domain))

        retypes = pipeline.lossiness_summary()["retype"]
        intended = (not preset.oop.aggregation.enabled) and bool(_pairs(domain, "aggregation"))
        if intended:
            assert retypes > 0, "expected the documented aggregation collapse, got none"
        else:
            assert retypes == 0, (
                f"{domain_path} x {preset_name}: {retypes} retype action(s) with no preset "
                f"reason for them - {[a['step'] for a in pipeline.action_log]}"
            )

    @pytest.mark.skipif(not is_javac_available(), reason="javac not on PATH")
    @pytest.mark.parametrize("domain_path,preset_name", MATRIX)
    def test_repaired_hint_graph_is_javac_legal(self, domain_path, preset_name):
        """Every shipped combination, held to the same oracle as everything else. Catches a
        config change that produces a structurally illegal graph before any API call is spent."""
        domain = DomainConfig(**_raw(domain_path))
        repaired = StructuralRepairPipeline(_preset(preset_name), domain).repair(sketch_from_hints(domain))
        ok, stderr = SkeletonGate().check(repaired)
        assert ok is not False, f"{domain_path} x {preset_name} is not javac-legal:\n{stderr}"
