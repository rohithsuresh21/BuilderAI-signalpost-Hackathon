#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description='Run 100-company smoke test')
    parser.add_argument('--organisations', default='smoke-companies.jsonl')
    parser.add_argument('--bulk', default='brreg-enheter.csv.gz')
    parser.add_argument('--run-id', default='smoke-100')
    args = parser.parse_args()

    script = Path('scripts/run_competition_batch.py')
    if not script.exists():
        script = Path('signalpost-starter-kit/scripts/run_competition_batch.py')

    cmd = [
        sys.executable,
        str(script),
        '--organisations', args.organisations,
        '--bulk', args.bulk,
        '--profiles-output', 'out/smoke-profiles.jsonl',
        '--output', 'out/smoke-envelopes.jsonl',
        '--report', 'out/smoke-report.json',
        '--run-id', args.run_id,
        '--expected-count', '100',
    ]
    subprocess.run(cmd, check=False)
    report_path = Path('out/smoke-report.json')
    if report_path.exists():
        with open(report_path) as f:
            report = json.load(f)
        print(f\"Envelopes emitted: {report.get('emitted_envelopes')}\")
        print(f\"Validation passed: {report.get('validation', {}).get('passed')}\")
