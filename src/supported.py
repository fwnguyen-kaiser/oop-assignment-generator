"""The declared, finite input space this generator is verified over.

A completeness claim (L1 in the README's epistemic table) needs a BOUNDED space to
exhaust. "Any domain an instructor might invent" is unbounded by construction, so no
completeness claim over it is even possible - which is precisely what pins most of this
system at L4 permanently, and no amount of extra fuzzing changes that.

Declaring the supported space as a closed set of shipped configs is what makes the
preset-boundary requirements finite, and therefore checkable at all: 5 domains x 3
presets = 15 combinations, enumerable in a test rather than argued about in prose.

Anything outside this set still runs. It is simply outside what has been verified, and
run_all.py says so out loud instead of implying coverage that does not exist.
"""
import os

SUPPORTED_DOMAINS = (
    "configs/domains/animal.yaml",
    "configs/domains/banking.yaml",
    "configs/domains/e_commerce.yaml",
    "configs/domains/library.yaml",
    "configs/domains/rpg_game.yaml",
)

SUPPORTED_PRESETS = (
    "configs/presets/beginner.yaml",
    "configs/presets/intermediate.yaml",
    "configs/presets/advanced.yaml",
)


def _norm(path: str) -> str:
    """Compare by forward-slashed relative path so a Windows backslash argument, an
    absolute path, and the literal string in the tuples above all agree."""
    p = os.path.normpath(os.path.abspath(path)).replace(os.sep, "/")
    root = os.path.normpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).replace(os.sep, "/")
    if p.startswith(root + "/"):
        p = p[len(root) + 1:]
    return p


def is_supported(domain_path: str, preset_path: str) -> bool:
    return _norm(domain_path) in SUPPORTED_DOMAINS and _norm(preset_path) in SUPPORTED_PRESETS


def supported_matrix():
    """The 15 (domain, preset) pairs the verified claims are scoped to."""
    return [(d, p) for d in SUPPORTED_DOMAINS for p in SUPPORTED_PRESETS]
