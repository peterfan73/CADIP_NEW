#!/usr/bin/env python3
"""
CADIP Upload Script — Transfers web report files to inta-ddp via FTP.

Pipeline:
  1. IN/         → compute MD5, rename to FILENAME_<hash>, move to MD5/
  2. Remote      → download *_BAD files, requeue originals from Sent_Files/ to MD5/
  3. MD5/        → FTP upload (verify 226), move to Sent_Files/

Usage:
    python3 cadip_upload.py [--config PATH] [--dry-run]

Requires: Python 3.6+ (stdlib only, no pip dependencies)
"""

import argparse
import configparser
import ftplib
import hashlib
import logging
import os
import re
import shutil
import signal
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path):
    """Load and validate the INI config file."""
    if not os.path.isfile(config_path):
        raise FileNotFoundError("Config file not found: {}".format(config_path))

    config = configparser.ConfigParser()
    config.read(config_path)

    required = {
        'ftp': ['host', 'user', 'password'],
        'paths': [
            'cadip_base', 'in_dir', 'md5_dir', 'sent_files_dir',
            'bad_from_ddp_dir', 'ftp_results_dir', 'lock_file',
        ],
        'remote': ['target_path_template'],
    }
    for section, keys in required.items():
        if not config.has_section(section):
            raise ValueError("Missing config section: [{}]".format(section))
        for key in keys:
            if not config.get(section, key, fallback=None):
                raise ValueError(
                    "Missing config key: [{}] {}".format(section, key)
                )

    return config


# ---------------------------------------------------------------------------
# Lock file (prevents overlapping cron runs)
# ---------------------------------------------------------------------------

def acquire_lock(lock_path, logger):
    """
    Create a PID-based lock file. Returns True if lock acquired.
    Detects and removes stale locks from dead processes.
    """
    if os.path.isfile(lock_path):
        try:
            with open(lock_path, 'r') as f:
                old_pid = int(f.read().strip())
            # Check if the old process is still alive
            os.kill(old_pid, 0)
            # Process is alive — another instance is running
            logger.warning(
                "Another instance is running (PID {}). Exiting.".format(old_pid)
            )
            return False
        except (ValueError, OSError):
            # PID file is corrupt or process is dead — stale lock
            logger.info("Removing stale lock file (old PID).")
            os.remove(lock_path)

    # Write our PID
    with open(lock_path, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock(lock_path):
    """Remove the lock file."""
    try:
        os.remove(lock_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(ftp_results_dir):
    """Configure logging to both stdout and a timestamped log file."""
    os.makedirs(ftp_results_dir, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%S")
    log_file = os.path.join(ftp_results_dir, "cadip_20{}.log".format(now))

    logger = logging.getLogger("cadip")
    logger.setLevel(logging.DEBUG)

    # File handler — detailed
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler — concise
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info("Log file: {}".format(log_file))
    return logger


# ---------------------------------------------------------------------------
# FTP helpers
# ---------------------------------------------------------------------------

def ftp_connect(config, logger):
    """Open an FTP connection and login. Returns (ftplib.FTP, home_dir)."""
    host = config.get('ftp', 'host')
    user = config.get('ftp', 'user')
    passwd = config.get('ftp', 'password')

    logger.info("Connecting to {}...".format(host))
    ftp = ftplib.FTP(host, timeout=30)
    ftp.login(user, passwd)
    home_dir = ftp.pwd()
    logger.info("Connected and logged in. Home: {}".format(home_dir))
    return ftp, home_dir


def ftp_upload_file(ftp, home_dir, local_path, remote_dir, logger):
    """
    Upload a single file to the remote directory via FTP.
    Returns True on success (226 response), False on failure.
    """
    filename = os.path.basename(local_path)
    try:
        ftp.cwd(home_dir)
        ftp.cwd(remote_dir)
        with open(local_path, 'rb') as f:
            resp = ftp.storbinary("STOR {}".format(filename), f)
        logger.debug("FTP response for {}: {}".format(filename, resp))

        if resp.startswith("226"):
            logger.info("  Upload OK: {}".format(filename))
            return True
        else:
            logger.warning(
                "  Upload uncertain for {}: {}".format(filename, resp)
            )
            return False

    except ftplib.all_errors as e:
        logger.error("  Upload FAILED for {}: {}".format(filename, e))
        return False


def extract_unit_from_filename(filename):
    """
    Extract the DFEP processing unit number from an EOF filename.
    Filename format: Sxy_OPER_REP_PASS_n_MPS__..._V..._....EOF[_md5hash]
    Returns the unit string (e.g. '4'), or None if not found.
    """
    match = re.match(r'S\d[A-D]_OPER_REP_PASS_(\d)_', filename)
    if match:
        return match.group(1)
    return None


def ftp_download_bad_files(ftp, home_dir, target_path_template, local_dir, logger):
    """
    Download all *_BAD files from all DFEP directories (units 1-9).
    Returns list of downloaded filenames.
    """
    downloaded = []
    os.makedirs(local_dir, exist_ok=True)
    operational_DFEPs = [4,5]
    for unit in operational_DFEPs:
        remote_dir = target_path_template.format(unit=unit)
        try:
            ftp.cwd(home_dir)
            ftp.cwd(remote_dir)
            file_list = ftp.nlst()
        except ftplib.all_errors:
            # Directory may not exist for this unit — skip silently
            continue

        bad_files = [f for f in file_list if f.endswith("_BAD")]
        for filename in bad_files:
            local_path = os.path.join(local_dir, filename)
            try:
                with open(local_path, 'wb') as f:
                    ftp.retrbinary("RETR {}".format(filename), f.write)
                logger.info(
                    "  Downloaded BAD file from DFEP{}: {}".format(
                        unit, filename
                    )
                )
                downloaded.append(filename)
            except ftplib.all_errors as e:
                logger.error(
                    "  Failed to download {}: {}".format(filename, e)
                )

    if not downloaded:
        logger.info("No BAD files found on remote server.")

    return downloaded


# ---------------------------------------------------------------------------
# Step 1: Process new files in IN/ → MD5/
# ---------------------------------------------------------------------------

def compute_md5(filepath):
    """Compute MD5 hex digest of a file."""
    # usedforsecurity=False is needed on systems with strict OpenSSL (FIPS).
    # Python 3.6 does not support this kwarg, so we fall back gracefully.
    try:
        md5 = hashlib.md5(usedforsecurity=False)
    except TypeError:
        md5 = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5.update(chunk)
    return md5.hexdigest()


def process_incoming_files(in_dir, md5_dir, logger):
    """
    For each file in IN/:
      - compute MD5 hash
      - rename to FILENAME_<md5hash>
      - move to MD5/
    """
    if not os.path.isdir(in_dir):
        logger.debug("IN directory does not exist: {}".format(in_dir))
        return

    os.makedirs(md5_dir, exist_ok=True)

    files = [f for f in os.listdir(in_dir)
             if os.path.isfile(os.path.join(in_dir, f))]

    if not files:
        logger.info("No new files in IN/.")
        return

    for filename in sorted(files):
        src_path = os.path.join(in_dir, filename)
        md5_hash = compute_md5(src_path)
        new_name = "{}_{}".format(filename, md5_hash)
        dest_path = os.path.join(md5_dir, new_name)

        shutil.move(src_path, dest_path)
        logger.info("  {} -> {}".format(filename, new_name))


# ---------------------------------------------------------------------------
# Step 2: Download BAD files and requeue
# ---------------------------------------------------------------------------

def requeue_bad_files(bad_from_ddp_dir, sent_files_dir, md5_dir, logger):
    """
    For each *_BAD file in BAD_From_DDP/:
      - find the original (strip _BAD suffix) in Sent_Files/
      - move it back to MD5/ for re-upload
      - delete the BAD marker file
    """
    if not os.path.isdir(bad_from_ddp_dir):
        return

    for bad_file in sorted(os.listdir(bad_from_ddp_dir)):
        bad_path = os.path.join(bad_from_ddp_dir, bad_file)
        if not os.path.isfile(bad_path):
            continue

        # Original filename = BAD filename without _BAD suffix
        if not bad_file.endswith("_BAD"):
            continue
        original_name = bad_file[:-4]

        sent_path = os.path.join(sent_files_dir, original_name)
        if os.path.isfile(sent_path):
            dest_path = os.path.join(md5_dir, original_name)
            shutil.move(sent_path, dest_path)
            logger.info("  Requeued for re-upload: {}".format(original_name))
        else:
            logger.warning(
                "  BAD file {} received, but original {} not found in "
                "Sent_Files/".format(bad_file, original_name)
            )

        os.remove(bad_path)
        logger.debug("  Removed BAD marker: {}".format(bad_file))


# ---------------------------------------------------------------------------
# Step 3: Upload files from MD5/ to remote
# ---------------------------------------------------------------------------

def upload_pending_files(ftp, home_dir, md5_dir, sent_files_dir,
                        target_path_template, logger):
    """
    Upload all files in MD5/ to the remote server.
    The remote directory is determined per file from the processing unit
    number in the filename.
    On success (226): move to Sent_DDP/.
    On failure: leave in MD5/ for retry on next run.
    """
    if not os.path.isdir(md5_dir):
        logger.debug("MD5 directory does not exist: {}".format(md5_dir))
        return

    os.makedirs(sent_files_dir, exist_ok=True)

    files = [f for f in os.listdir(md5_dir)
             if os.path.isfile(os.path.join(md5_dir, f))]

    if not files:
        logger.info("No pending files to upload.")
        return

    logger.info("Uploading {} file(s)...".format(len(files)))
    for filename in sorted(files):
        filepath = os.path.join(md5_dir, filename)

        # Determine target directory from filename
        unit = extract_unit_from_filename(filename)
        if unit is None:
            logger.warning(
                "  Cannot determine DFEP unit from filename: {}".format(
                    filename
                )
            )
            logger.warning("  Kept in MD5/ for manual review.")
            continue

        target_path = target_path_template.format(unit=unit)
        logger.debug(
            "  Target for {}: {}".format(filename, target_path)
        )

        success = ftp_upload_file(
            ftp, home_dir, filepath, target_path, logger
        )
        if success:
            shutil.move(
                filepath,
                os.path.join(sent_files_dir, filename)
            )
            logger.info("  Moved to Sent_DDP/: {}".format(filename))
        else:
            logger.warning(
                "  Kept in MD5/ for retry: {}".format(filename)
            )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CADIP web report upload to inta-ddp via FTP"
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "cadip.ini"),
        help="Path to cadip.ini config file (default: same dir as script)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Process IN/ files but skip all FTP operations"
    )
    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)

    # Read paths from config
    in_dir = config.get('paths', 'in_dir')
    md5_dir = config.get('paths', 'md5_dir')
    sent_files_dir = config.get('paths', 'sent_files_dir')
    bad_from_ddp_dir = config.get('paths', 'bad_from_ddp_dir')
    ftp_results_dir = config.get('paths', 'ftp_results_dir')
    lock_path = config.get('paths', 'lock_file')
    target_path_template = config.get('remote', 'target_path_template')

    # Setup logging
    logger = setup_logging(ftp_results_dir)

    logger.info("=" * 60)
    logger.info("CADIP Upload — Starting")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("*** DRY-RUN MODE — no FTP operations ***")

    # Acquire lock
    if not acquire_lock(lock_path, logger):
        sys.exit(0)

    # Ensure lock is released on exit (normal, exception, or signal)
    def cleanup(signum=None, frame=None):
        release_lock(lock_path)
        if signum is not None:
            sys.exit(1)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        # --- Step 1: Process new files in IN/ → MD5/ ---
        logger.info("Step 1: Processing new files in IN/...")
        process_incoming_files(in_dir, md5_dir, logger)

        if args.dry_run:
            # Show what would be uploaded
            if os.path.isdir(md5_dir):
                pending = [f for f in os.listdir(md5_dir)
                           if os.path.isfile(os.path.join(md5_dir, f))]
                logger.info(
                    "Pending files for upload: {}".format(len(pending))
                )
                for f in sorted(pending):
                    logger.info("  Would upload: {}".format(f))
            logger.info("DRY-RUN complete. No FTP operations performed.")
            return

        # --- FTP connection ---
        try:
            ftp, home_dir = ftp_connect(config, logger)
        except ftplib.all_errors as e:
            logger.error("FTP connection failed: {}".format(e))
            sys.exit(1)

        try:
            # --- Step 2: Download BAD files, requeue for re-upload ---
            logger.info("Step 2: Checking for BAD files on remote...")
            downloaded_bad = ftp_download_bad_files(
                ftp, home_dir, target_path_template,
                bad_from_ddp_dir, logger
            )
            if downloaded_bad:
                logger.info("Requeuing BAD files for re-upload...")
                requeue_bad_files(
                    bad_from_ddp_dir, sent_files_dir, md5_dir, logger
                )

            # --- Step 3: Upload pending files from MD5/ ---
            logger.info("Step 3: Uploading pending files...")
            upload_pending_files(
                ftp, home_dir, md5_dir, sent_files_dir,
                target_path_template, logger
            )

        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
            logger.info("FTP connection closed.")

    finally:
        release_lock(lock_path)

    logger.info("CADIP Upload — Finished")


if __name__ == "__main__":
    main()
