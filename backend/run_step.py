#!/usr/bin/env python3
"""Run and test a single pipeline step from the command line.

Each step reads its inputs from a shared --state JSON file and writes its
outputs back into it, so you can run the steps one at a time and inspect each
result independently.

Examples
--------
Seed the state with a script and run step 1:
    python run_step.py 1 --script-file fixtures/sample_script.txt --state /tmp/s.json

Run the remaining steps, each reading the growing state file:
    python run_step.py 2 --state /tmp/s.json
    python run_step.py 3 --state /tmp/s.json
    python run_step.py 4 --state /tmp/s.json
    python run_step.py 5 --state /tmp/s.json

Test the wiring without an API key (canned outputs):
    python run_step.py 1 --script "A lone lighthouse..." --state /tmp/s.json --mock
    python run_step.py 2 --state /tmp/s.json --mock   # ... and so on

You can also hand-edit /tmp/s.json between steps to test a step with custom
inputs.
"""

import argparse
import json
import os
import sys

from mock_gemini import make_mock
from pipeline import STEP_INPUTS, STEP_TITLES, run_step


def load_state(path: str) -> dict:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path: str, state: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one pipeline step in isolation.")
    parser.add_argument("step", type=int, choices=range(1, 7), help="Step number 1-6")
    parser.add_argument("--state", help="Shared JSON state file (read inputs / write outputs)")
    parser.add_argument("--script", help="Seed the script directly (step 1)")
    parser.add_argument("--script-file", help="Seed the script from a text file (step 1)")
    parser.add_argument("--in", dest="infile", help="Read the full input payload from this JSON file instead of --state")
    parser.add_argument("--mock", action="store_true", help="Use canned responses (no API key needed)")
    args = parser.parse_args()

    # Build the payload.
    if args.infile:
        with open(args.infile, encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = load_state(args.state)

    if args.script:
        state["script"] = args.script
    if args.script_file:
        with open(args.script_file, encoding="utf-8") as f:
            state["script"] = f.read()

    # Validate inputs are present before calling out.
    needed = STEP_INPUTS[args.step]
    missing = [k for k in needed if not state.get(k)]
    if missing:
        print(
            f"Step {args.step} ({STEP_TITLES[args.step]}) needs: {', '.join(needed)}.\n"
            f"Missing from state: {', '.join(missing)}.\n"
            f"Seed earlier steps first (or hand-edit the state file).",
            file=sys.stderr,
        )
        return 2

    gemini = make_mock() if args.mock else None
    try:
        output = run_step(args.step, state, gemini=gemini)
    except Exception as exc:  # noqa: BLE001
        print(f"Step {args.step} failed: {exc}", file=sys.stderr)
        return 1

    # Merge outputs back into the running state and persist.
    state.update(output)
    save_state(args.state, state)

    print(f"--- Step {args.step}: {STEP_TITLES[args.step]} ---", file=sys.stderr)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.state:
        print(f"\nState saved to {args.state}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
