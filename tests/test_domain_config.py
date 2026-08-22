import pytest
import yaml
import glob
from src.schemas.domain import DomainConfig, MAX_ENTITY_HINTS


def test_all_shipped_domains_load_within_the_verified_envelope():
    for path in glob.glob("configs/domains/*.yaml"):
        with open(path, encoding="utf-8") as f:
            DomainConfig(**yaml.safe_load(f))  # must not raise


def test_rejects_a_domain_exceeding_the_verified_hint_count():
    """The bound was chosen from measure_lossiness.py's actual measured envelope
    (hint_count:max_classes ratio up to 2.0, verified 6.7% lossy) rather than picked
    arbitrarily - domain YAML is an author-controlled asset in this project, not
    adversarial input, so it's cheaper and more honest to keep every domain inside the
    tested range than to try to prove the prompt fix generalizes to an unverified ratio."""
    with pytest.raises(Exception):
        DomainConfig(
            name="x", description="x", keywords=[], style="x",
            entity_hints={"core": [f"Entity{i}" for i in range(MAX_ENTITY_HINTS + 1)]},
        )


def test_accepts_a_domain_exactly_at_the_bound():
    DomainConfig(
        name="x", description="x", keywords=[], style="x",
        entity_hints={"core": [f"Entity{i}" for i in range(MAX_ENTITY_HINTS)]},
    )
