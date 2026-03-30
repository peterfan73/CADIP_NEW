#!/usr/bin/env python3
"""
CADIP Main Orchestrator — runs report generation and upload in sequence.

This is the script to schedule via cron (e.g. every 2 minutes):
    */2 * * * * python3 /home/meos/bin/cadip/cadip_main.py

Pipeline:
  Step 1: cadip_report_generator  (scan acquisitions → generate EOF → IN/)
  Step 2: cadip_upload            (IN/ → MD5/ → FTP upload → Sent_DDP/)

Usage:
    python3 cadip_main.py [--config PATH] [--dry-run]

Requires: Python 3.6+ (stdlib only, no pip dependencies)
"""

import argparse
import os
import sys

# Ensure the script directory is in the Python path so we can import siblings
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cadip_report_generator
import cadip_upload


def main():
    parser = argparse.ArgumentParser(
        description="CADIP Orchestrator — report generation + upload"
    )
    parser.add_argument(
        "--config",
        default=os.path.join(SCRIPT_DIR, "cadip.ini"),
        help="Path to cadip.ini config file (default: same dir as script)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run both steps in dry-run mode (no FTP, no file generation)"
    )
    args = parser.parse_args()

    # --- Step 1: Generate reports ---
    config = cadip_report_generator.load_config(args.config)
    cadip_report_generator.run(config, dry_run=args.dry_run)

    # --- Step 2: Upload to DDP ---
    # cadip_upload.main() handles its own argument parsing,
    # so we build the sys.argv it expects.
    upload_args = ['cadip_upload.py', '--config', args.config]
    if args.dry_run:
        upload_args.append('--dry-run')

    sys.argv = upload_args
    cadip_upload.main()


if __name__ == "__main__":
    main()
