"""Cheap harness to decide whether Phase 2.fb (a constraint-aware LLM retry on lossy
structural repair) is worth building. Runs ONLY Phase 1 -> 1b/1c -> 2 -> 2.5 -> 3 -> 4
(src.pipeline.run_pipeline) across domains x presets - deliberately never touches
src.detail_pipeline (Phase 5a/6, the expensive body-writing calls), since the
question "does repair_pipeline.py destroy/invent structure the LLM proposed" is
answered entirely by Phase 1+2 and doesn't depend on anything downstream.

PRE-REGISTERED THRESHOLD (fixed before running anything, not tuned after seeing
results - see feedback_verification_discipline memory on why):
    A run is "lossy" if repair_pipeline.py logged at least one action classified
    invent_or_destroy in LOSSINESS (repair_pipeline.py) for that run.
    - < 15% of runs lossy  -> do NOT build Phase 2.fb (rare enough not to be worth
      an extra LLM call on every generation)
    - > 35% of runs lossy  -> DO build Phase 2.fb
    - 15-35%               -> genuinely ambiguous, needs a human judgment call at
      that point (not resolved by this measurement alone)

Usage: python measure_lossiness.py [trials_per_combo]  (default 2 -> 5 domains x 3
presets x 2 = 30 runs, matching the N=30 the threshold above was set for)
"""
import glob
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from src.pipeline import run_pipeline

LOSSINESS_LOG = "output/phase2_lossiness_log.jsonl"
LOSSY_LOW = 0.15
LOSSY_HIGH = 0.35


def main():
    trials_per_combo = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    domains = sorted(glob.glob("configs/domains/*.yaml"))
    presets = sorted(glob.glob("configs/presets/*.yaml"))

    # Start from a clean log so this measurement isn't polluted by earlier runs.
    os.makedirs("output", exist_ok=True)
    if os.path.exists(LOSSINESS_LOG):
        os.rename(LOSSINESS_LOG, LOSSINESS_LOG + ".bak")

    total_planned = len(domains) * len(presets) * trials_per_combo
    print(f"=== Phase 2.fb measurement: {len(domains)} domains x {len(presets)} presets x {trials_per_combo} trials = {total_planned} runs ===")
    print(f"Pre-registered threshold: <{LOSSY_LOW:.0%} lossy -> skip 2.fb, >{LOSSY_HIGH:.0%} -> build 2.fb, between -> judgment call\n")

    completed = 0
    failed = 0
    for domain_path in domains:
        for preset_path in presets:
            for trial in range(trials_per_combo):
                print(f"\n--- [{completed + failed + 1}/{total_planned}] {domain_path} x {preset_path} (trial {trial + 1}) ---")
                try:
                    run_pipeline(domain_path, preset_path)
                    completed += 1
                except Exception as e:
                    failed += 1
                    print(f"[MEASUREMENT] run failed, skipping: {e}")

    print(f"\n=== {completed} runs completed, {failed} failed ===")

    if not os.path.exists(LOSSINESS_LOG):
        print("No lossiness data was recorded - nothing to analyze.")
        return

    rows = []
    with open(LOSSINESS_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    n = len(rows)
    if n == 0:
        print("Lossiness log is empty - nothing to analyze.")
        return

    lossy_runs = [r for r in rows if r.get("invent_or_destroy", 0) > 0]
    pct_lossy = len(lossy_runs) / n

    print(f"\n=== RESULT ===")
    print(f"N = {n} runs with recorded lossiness data")
    print(f"Lossy runs (>=1 invent_or_destroy action): {len(lossy_runs)} ({pct_lossy:.1%})")
    for key in ("normalize", "retype", "drop_edge", "invent_or_destroy"):
        avg = sum(r.get(key, 0) for r in rows) / n
        print(f"  avg {key} actions/run: {avg:.2f}")

    if pct_lossy < LOSSY_LOW:
        print(f"\n-> {pct_lossy:.1%} < {LOSSY_LOW:.0%}: DO NOT build Phase 2.fb (per pre-registered threshold).")
    elif pct_lossy > LOSSY_HIGH:
        print(f"\n-> {pct_lossy:.1%} > {LOSSY_HIGH:.0%}: BUILD Phase 2.fb (per pre-registered threshold).")
    else:
        print(f"\n-> {pct_lossy:.1%} is in the ambiguous {LOSSY_LOW:.0%}-{LOSSY_HIGH:.0%} zone - needs a human judgment call, not auto-resolved.")


if __name__ == "__main__":
    main()
