from pydantic import BaseModel, model_validator
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

# generate_sketch's prompt tells the LLM to pick at most `max_classes` entities out of
# however many entity_hints exist, rather than trying to include them all - this only
# has real measured evidence behind it up to a hint_count:max_classes ratio of 2.0
# (8 hints against beginner preset's max_classes=4, the tightest case among the 5
# shipped domains): 30 live-Gemini trials before the prompt fix showed 20% of runs
# destructively dropping an entity to fit the limit, 6.7% after - see
# measure_lossiness.py and README's "Measured Before Building: Phase 2.fb" section.
# Nothing was tested beyond that ratio. Rather than trying to prove the prompt fix
# generalizes to an arbitrary ratio (unfalsifiable without spending real API calls on
# every possible domain), domain YAML is an author-controlled asset in this project,
# not adversarial input - so the simpler, honest fix is to keep every domain inside
# the verified envelope and let this validator catch anything that would silently
# exceed it, rather than deriving degraded/unverified behavior no one asked for.
MAX_ENTITY_HINTS = 8

class DomainConfig(BaseModel):
    name: str
    description: str
    keywords: List[str]
    entity_hints: Optional[EntityHints] = None
    relationship_hints: Optional[RelationshipHints] = None
    style: str

    @model_validator(mode="after")
    def check_hint_count_within_verified_envelope(self):
        if self.entity_hints:
            total = len(self.entity_hints.core) + len(self.entity_hints.optional or [])
            if total > MAX_ENTITY_HINTS:
                raise ValueError(
                    f"entity_hints has {total} names (core+optional), but the class-count "
                    f"prompt guidance has only been measured live up to {MAX_ENTITY_HINTS} "
                    f"hints against the smallest preset's max_classes - see MAX_ENTITY_HINTS's "
                    f"docstring in src/schemas/domain.py. Trim entity_hints, or re-run "
                    f"measure_lossiness.py against this domain and raise the bound once it's "
                    f"actually been verified, rather than shipping an unverified ratio."
                )
        return self
