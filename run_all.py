import time
import os
import sys

# Ensure the root directory is in the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.pipeline import run_pipeline
from src.detail_pipeline import run_detail_pipeline

# ==========================================
# ⚙️ QUICK CONFIGURATION
# Edit these paths to change the assignment
# ==========================================
DOMAIN_CONFIG = "configs/domains/library.yaml"   # e.g., library.yaml, e_commerce.yaml
PRESET_CONFIG = "configs/presets/advanced.yaml"

if __name__ == "__main__":
    # If passed via command line, it will override the quick config above
    domain_path = sys.argv[1] if len(sys.argv) > 1 else DOMAIN_CONFIG
    preset_path = sys.argv[2] if len(sys.argv) > 2 else PRESET_CONFIG

    print(f"=== STARTING PHASE 1-3 (STRUCTURAL) ===")
    print(f"Domain: {domain_path}")
    print(f"Preset: {preset_path}")
    run_pipeline(domain_path, preset_path)
    
    print("\n=== STARTING PHASE 4-5 (DETAILS & RENDERING) ===")
    run_detail_pipeline(domain_path, preset_path)
    
    print("\n=== ALL PHASES COMPLETED ===")
