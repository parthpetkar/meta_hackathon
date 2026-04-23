#!/usr/bin/env python3
"""Quick analysis script to visualize difficulty progression issues."""

import re
from pathlib import Path


def parse_inference_log(log_path: Path):
    """Extract episode data from inference log."""
    episodes = []
    
    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Find all episode starts
    episode_pattern = r'\[EPISODE START\] label=(\w+)\s+fault=(\w+)\s+stage=\S+\s+difficulty=(\w+)'
    episode_matches = re.finditer(episode_pattern, content)
    
    # Find all task completions
    completion_pattern = r'=== TASK COMPLETED === Success: (\w+) \| Resolved: (\w+) \| Score: ([\d.]+) \| Steps: (\d+)'
    completion_matches = re.finditer(completion_pattern, content)
    
    episodes_data = list(episode_matches)
    completions_data = list(completion_matches)
    
    results = []
    for i, (ep, comp) in enumerate(zip(episodes_data, completions_data), 1):
        label = ep.group(1)
        fault = ep.group(2)
        difficulty = ep.group(3)
        success = comp.group(1)
        score = float(comp.group(3))
        steps = int(comp.group(4))
        
        results.append({
            'episode': i,
            'label': label,
            'fault': fault,
            'difficulty': difficulty,
            'score': score,
            'steps': steps,
            'success': success == 'True'
        })
    
    return results


def analyze_difficulty_progression(episodes):
    """Analyze and display difficulty progression issues."""
    print("=" * 80)
    print("DIFFICULTY PROGRESSION ANALYSIS")
    print("=" * 80)
    print()
    
    print("Episode | Fault Type          | Difficulty | Score | Steps | Success")
    print("-" * 80)
    
    for ep in episodes:
        print(f"{ep['episode']:7d} | {ep['fault']:19s} | {ep['difficulty']:10s} | {ep['score']:.3f} | {ep['steps']:5d} | {ep['success']}")
    
    print()
    print("=" * 80)
    print("ISSUES DETECTED:")
    print("=" * 80)
    print()
    
    # Check for stagnation
    difficulty_map = {'easy': 0.20, 'medium': 0.41, 'hard': 0.70}
    difficulties = [difficulty_map.get(ep['difficulty'], 0) for ep in episodes]
    scores = [ep['score'] for ep in episodes]
    
    stagnant_count = 0
    for i in range(1, len(episodes)):
        if difficulties[i] == difficulties[i-1] and scores[i-1] > 0.60:
            stagnant_count += 1
            print(f"⚠️  Episode {i}: Difficulty STAGNANT at {difficulties[i]:.2f} despite score {scores[i-1]:.3f} > 0.60")
    
    print()
    if stagnant_count > 0:
        print(f"❌ Found {stagnant_count} episodes with stagnant difficulty despite good performance")
        print()
        print("EXPECTED BEHAVIOR:")
        print("  When score > 0.60 (neutral threshold), difficulty should INCREASE")
        print("  When score < 0.60, difficulty should DECREASE")
        print()
        print("ROOT CAUSE:")
        print("  - _EMA_ALPHA too low (0.20) → slow adaptation")
        print("  - _STEP_CAP too small (0.08) → tiny increments")
        print()
        print("FIXES APPLIED:")
        print("  ✅ Increased _STEP_CAP from 0.08 to 0.15")
        print("  ✅ Increased _EMA_ALPHA from 0.20 to 0.35")
    else:
        print("✅ No difficulty stagnation detected")
    
    print()
    
    # Check for verify_fix skipping
    print("=" * 80)
    print("VERIFY_FIX COMPLIANCE:")
    print("=" * 80)
    print()
    print("(Check the full log for -0.05 penalties on finalize steps)")
    print()


if __name__ == "__main__":
    # Find the most recent inference log
    results_dir = Path("results")
    log_files = sorted(results_dir.glob("inference_*.log"), reverse=True)
    
    if not log_files:
        print("No inference logs found in results/")
        exit(1)
    
    latest_log = log_files[0]
    print(f"Analyzing: {latest_log}")
    print()
    
    episodes = parse_inference_log(latest_log)
    analyze_difficulty_progression(episodes)
