import json
import os
import sys

# Ensure the root directory is in the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.pipeline import run_pipeline
from src.detail_pipeline import run_detail_pipeline
from src.supported import is_supported, supported_matrix

# ==========================================
# QUICK CONFIGURATION
# Edit these paths to change the assignment
# ==========================================
DOMAIN_CONFIG = "configs/domains/library.yaml"   # e.g., library.yaml, e_commerce.yaml
PRESET_CONFIG = "configs/presets/advanced.yaml"

CONFORMANCE_PATH = "output/conformance_report.json"


def main() -> int:
    # If passed via command line, it will override the quick config above
    domain_path = sys.argv[1] if len(sys.argv) > 1 else DOMAIN_CONFIG
    preset_path = sys.argv[2] if len(sys.argv) > 2 else PRESET_CONFIG

    print("=== STARTING PHASE 1-3 (STRUCTURAL) ===")
    print(f"Domain: {domain_path}")
    print(f"Preset: {preset_path}")
    if not is_supported(domain_path, preset_path):
        # Not an error - just an honest scope statement. Every verified claim in the README
        # is scoped to the declared matrix in src/supported.py; outside it the pipeline still
        # runs, but nothing has been measured, so saying nothing here would imply coverage
        # that does not exist.
        print(
            f"[SCOPE] This (domain, preset) pair is OUTSIDE the declared verified matrix "
            f"({len(supported_matrix())} combinations - see src/supported.py). It will run, "
            f"but no verified claim covers it."
        )

    run_pipeline(domain_path, preset_path)

    print("\n=== STARTING PHASE 4-7 (DETAILS & RENDERING) ===")
    run_detail_pipeline(domain_path, preset_path)

    print("\n=== ALL PHASES COMPLETED ===")

    # Read the conformance verdict rather than leaving it as a file nobody opens. A run that
    # violated the instructor's preset must not exit looking identical to a clean one - that
    # stdout-only-signal shape is exactly the defect an audit already found at Phase 6.
    if not os.path.exists(CONFORMANCE_PATH):
        print(f"[CONFORMANCE] {CONFORMANCE_PATH} missing - cannot confirm the preset was met.")
        return 1

    with open(CONFORMANCE_PATH, "r", encoding="utf-8") as f:
        conformance = json.load(f)

    if conformance.get("all_satisfied"):
        print(f"[CONFORMANCE] All {len(conformance['checks'])} preset requirements satisfied.")
        return 0

    print("[CONFORMANCE] FAILED - the generated assignment does not satisfy the preset:")
    for check in conformance.get("checks", []):
        if not check.get("satisfied"):
            print(f"  - {check['requirement']}: {check['detail']}")
    print(f"Files were still written to output/ - see {CONFORMANCE_PATH} for the full verdict.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
