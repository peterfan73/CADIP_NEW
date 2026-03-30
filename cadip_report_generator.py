#!/usr/bin/env python3
"""
CADIP Report Generator — Scans acquisition folders, parses XML reports,
and generates REP_PASS EOF files for upload to inta-ddp.

Pipeline:
  /disk3/distribution/reports/SXY_xxxxxxxx/
    ├── reconstruct_xband1_ch1_VCDU1.xml  ──┐
    ├── reconstruct_xband2_ch2_VCDU1.xml  ──┤── parse → EOF file → IN/
    └── DCS_0n_Sxy_..._DSIB.xml          ──┘

Usage:
    python3 cadip_report_generator.py [--config PATH] [--dry-run]

Requires: Python 3.6+ (stdlib only, no pip dependencies)
"""

import argparse
import configparser
import glob
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(config_path):
    """Load and validate the INI config file."""
    if not os.path.isfile(config_path):
        raise FileNotFoundError(
            "Config file not found: {}".format(config_path)
        )

    config = configparser.ConfigParser()
    config.read(config_path)

    required = {
        'paths': ['cadip_base', 'in_dir'],
        'report_generator': ['reports_source', 'processed_file', 'station_id'],
    }
    for section, keys in required.items():
        if not config.has_section(section):
            raise ValueError(
                "Missing config section: [{}]".format(section)
            )
        for key in keys:
            if not config.get(section, key, fallback=None):
                raise ValueError(
                    "Missing config key: [{}] {}".format(section, key)
                )

    return config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(ftp_results_dir):
    """Configure logging to both stdout and a timestamped log file."""
    os.makedirs(ftp_results_dir, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%S")
    log_file = os.path.join(
        ftp_results_dir, "report_gen_20{}.log".format(now)
    )

    logger = logging.getLogger("cadip_report_gen")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info("Log file: {}".format(log_file))
    return logger


# ---------------------------------------------------------------------------
# Processed folders tracking
# ---------------------------------------------------------------------------

def load_processed_folders(processed_file, logger):
    """
    Load the processed folders file. Each line is: timestamp|folder_name
    Purges entries older than 3 days.
    Returns a dict of {folder_name: timestamp_str}.
    """
    entries = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)

    if not os.path.isfile(processed_file):
        return entries

    with open(processed_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or '|' not in line:
                continue
            parts = line.split('|', 1)
            try:
                ts = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ")
                ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    entries[parts[1]] = parts[0]
                else:
                    logger.debug(
                        "Purging old entry: {}".format(parts[1])
                    )
            except ValueError:
                continue

    return entries


def save_processed_folders(processed_file, entries):
    """Save the processed folders dict back to file."""
    with open(processed_file, 'w') as f:
        for folder_name, ts in sorted(entries.items()):
            f.write("{}|{}\n".format(ts, folder_name))


def mark_as_processed(entries, folder_name):
    """Add a folder to the processed entries dict."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries[folder_name] = now


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

# Pattern: SXY_ followed by 8 alphanumeric characters
FOLDER_PATTERN = re.compile(r'^S[12][A-D]_[A-Za-z0-9]{8}$')


def scan_acquisition_folders(reports_source, processed, logger):
    """
    Scan the reports source directory for new SXY_xxxxxxxx folders.
    Returns list of full paths of new (unprocessed) folders.
    """
    if not os.path.isdir(reports_source):
        logger.error(
            "Reports source directory not found: {}".format(reports_source)
        )
        return []

    new_folders = []
    for entry in sorted(os.listdir(reports_source)):
        full_path = os.path.join(reports_source, entry)
        if not os.path.isdir(full_path):
            continue
        if not FOLDER_PATTERN.match(entry):
            continue
        if entry in processed:
            continue
        new_folders.append(full_path)

    return new_folders


# ---------------------------------------------------------------------------
# XML parsing — reconstruct_xband files
# ---------------------------------------------------------------------------

def parse_reconstruct_xml(filepath, logger):
    """
    Parse a reconstruct_xbandN_chN_VCDU1.xml file.
    Extracts from <Status VCID="63"> (fill frames) and <Summary> (totals).

    Returns dict:
      {
        'status_63': {'NumFrames': int, 'RsUncorrectable': int, 'RsCorrectable': int},
        'summary':   {'NumFrames': int, 'RsUncorrectable': int, 'RsCorrectable': int},
      }
    or None on error.
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as e:
        logger.error("Failed to parse {}: {}".format(filepath, e))
        return None

    result = {'status_63': None, 'summary': None}

    # Find Status VCID="63"
    for status_elem in root.iter('Status'):
        if status_elem.get('VCID') == '63':
            result['status_63'] = {
                'NumFrames': int(status_elem.findtext('NumFrames', '0')),
                'RsUncorrectable': int(
                    status_elem.findtext('RsUncorrectable', '0')
                ),
                'RsCorrectable': int(
                    status_elem.findtext('RsCorrectable', '0')
                ),
            }
            break

    # Find Summary
    summary_elem = root.find('.//Summary')
    if summary_elem is not None:
        result['summary'] = {
            'NumFrames': int(summary_elem.findtext('NumFrames', '0')),
            'RsUncorrectable': int(
                summary_elem.findtext('RsUncorrectable', '0')
            ),
            'RsCorrectable': int(
                summary_elem.findtext('RsCorrectable', '0')
            ),
        }

    if result['status_63'] is None:
        logger.error(
            "No Status VCID=63 found in {}".format(filepath)
        )
        return None
    if result['summary'] is None:
        logger.error("No Summary found in {}".format(filepath))
        return None

    return result


# ---------------------------------------------------------------------------
# XML parsing — DCS DSIB files
# ---------------------------------------------------------------------------

def parse_dcs_xml(filepath, logger):
    """
    Parse a DCS_0n_Sxy_..._DSIB.xml file.
    Extracts time_start and time_stop.

    Returns dict:
      {
        'time_start': '2026-03-16T08:11:14Z',
        'time_stop':  '2026-03-16T08:20:16Z',
      }
    or None on error.
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as e:
        logger.error("Failed to parse {}: {}".format(filepath, e))
        return None

    time_start = root.findtext('time_start')
    time_stop = root.findtext('time_stop')

    if not time_start or not time_stop:
        logger.error(
            "Missing time_start/time_stop in {}".format(filepath)
        )
        return None

    return {
        'time_start': time_start,
        'time_stop': time_stop,
    }


def format_time_for_filename(iso_time):
    """
    Convert '2026-03-16T08:11:14Z' to '20260316T081114' for the EOF filename.
    """
    # Strip trailing Z, remove dashes and colons
    t = iso_time.rstrip('Z')
    return t.replace('-', '').replace(':', '')


# ---------------------------------------------------------------------------
# Extract folder metadata
# ---------------------------------------------------------------------------

def extract_folder_metadata(folder_path, logger):
    """
    From an acquisition folder, extract:
      - satellite mission (e.g. '1A')
      - DFEP unit number (e.g. '4')
      - time_start, time_stop from DCS
      - channel 1 and 2 data from reconstruct XMLs

    Returns dict with all metadata, or None on error.
    """
    folder_name = os.path.basename(folder_path)

    # Extract satellite mission from folder name: S1A_xxxxxxxx → '1A'
    mission = folder_name[1:3]

    # Find reconstruct XML files
    ch1_files = glob.glob(
        os.path.join(folder_path, 'reconstruct_xband1_ch1_VCDU1.xml')
    )
    ch2_files = glob.glob(
        os.path.join(folder_path, 'reconstruct_xband2_ch2_VCDU1.xml')
    )

    if not ch1_files or not ch2_files:
        logger.error(
            "Missing reconstruct XML files in {}".format(folder_path)
        )
        return None

    # Find DCS file — pattern: DCS_0n_Sxy_*_DSIB.xml
    dcs_files = glob.glob(
        os.path.join(folder_path, 'DCS_0*_DSIB.xml')
    )
    if not dcs_files:
        logger.error("No DCS DSIB file found in {}".format(folder_path))
        return None

    dcs_file = dcs_files[0]

    # Extract unit number from DCS filename: DCS_04_... → '4'
    dcs_basename = os.path.basename(dcs_file)
    unit_match = re.match(r'DCS_0(\d)_', dcs_basename)
    if not unit_match:
        logger.error(
            "Cannot extract unit number from {}".format(dcs_basename)
        )
        return None
    unit_number = unit_match.group(1)

    # Parse reconstruct XMLs
    ch1_data = parse_reconstruct_xml(ch1_files[0], logger)
    ch2_data = parse_reconstruct_xml(ch2_files[0], logger)
    if not ch1_data or not ch2_data:
        return None

    # Parse DCS XML
    dcs_data = parse_dcs_xml(dcs_file, logger)
    if not dcs_data:
        return None

    return {
        'mission': mission,
        'unit_number': unit_number,
        'time_start': dcs_data['time_start'],
        'time_stop': dcs_data['time_stop'],
        'ch1': ch1_data,
        'ch2': ch2_data,
    }


# ---------------------------------------------------------------------------
# EOF file generation
# ---------------------------------------------------------------------------

def build_channel_element(parent, channel_name, channel_data):
    """
    Build a <channel_N> XML element with total_frames and data_frames.
    """
    summary = channel_data['summary']
    status_63 = channel_data['status_63']

    ch_elem = ET.SubElement(parent, channel_name)

    # total_frames = Summary values (all VCIDs)
    total = ET.SubElement(ch_elem, 'total_frames')
    ET.SubElement(total, 'NumFrames').text = str(summary['NumFrames'])
    ET.SubElement(total, 'RsUncorrectable').text = str(
        summary['RsUncorrectable']
    )
    ET.SubElement(total, 'RsCorrectable').text = str(
        summary['RsCorrectable']
    )

    # data_frames = Summary minus Status VCID=63 (fill frames)
    data = ET.SubElement(ch_elem, 'data_frames')
    ET.SubElement(data, 'NumFrames').text = str(
        summary['NumFrames'] - status_63['NumFrames']
    )
    ET.SubElement(data, 'RsUncorrectable').text = str(
        summary['RsUncorrectable'] - status_63['RsUncorrectable']
    )
    ET.SubElement(data, 'RsCorrectable').text = str(
        summary['RsCorrectable'] - status_63['RsCorrectable']
    )


def generate_eof_xml(metadata):
    """
    Generate the REP_PASS XML content.
    Returns an ElementTree root element.
    """
    root = ET.Element('REP_PASS')
    build_channel_element(root, 'channel_1', metadata['ch1'])
    build_channel_element(root, 'channel_2', metadata['ch2'])
    return root


def build_eof_filename(metadata, station_id):
    """
    Build the EOF filename:
    Sxy_OPER_REP_PASS_n_MPS__date1_Vdate2_date3.EOF
    """
    mission = metadata['mission']
    unit = metadata['unit_number']
    date1 = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    date2 = format_time_for_filename(metadata['time_start'])
    date3 = format_time_for_filename(metadata['time_stop'])

    return "S{mission}_OPER_REP_PASS_{unit}_{station}__{date1}_V{date2}_{date3}.EOF".format(
        mission=mission,
        unit=unit,
        station=station_id,
        date1=date1,
        date2=date2,
        date3=date3,
    )


def indent_xml(elem, level=0):
    """Add pretty-print indentation to XML elements (Python 3.6 compat)."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    if level == 0:
        elem.tail = "\n"


def write_eof_file(root_elem, output_path, logger):
    """Write the XML tree to an EOF file with XML declaration."""
    indent_xml(root_elem)
    tree = ET.ElementTree(root_elem)
    tree.write(output_path, encoding='unicode', xml_declaration=True)
    logger.info("  Generated: {}".format(os.path.basename(output_path)))


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_folder(folder_path, in_dir, station_id, logger):
    """
    Process a single acquisition folder:
      1. Parse XMLs
      2. Generate REP_PASS XML
      3. Write EOF file to IN/

    Returns the generated EOF filename, or None on error.
    """
    folder_name = os.path.basename(folder_path)
    logger.info("Processing: {}".format(folder_name))

    # Extract all metadata
    metadata = extract_folder_metadata(folder_path, logger)
    if not metadata:
        logger.error(
            "  Failed to extract metadata from {}".format(folder_name)
        )
        return None

    # Generate XML content
    root_elem = generate_eof_xml(metadata)

    # Build filename and write
    eof_filename = build_eof_filename(metadata, station_id)
    os.makedirs(in_dir, exist_ok=True)
    output_path = os.path.join(in_dir, eof_filename)
    write_eof_file(root_elem, output_path, logger)

    return eof_filename


def run(config, dry_run=False, logger=None):
    """
    Main entry point for the report generator.
    Can be called directly or from the orchestrator.
    Returns the number of files generated.
    """
    reports_source = config.get('report_generator', 'reports_source')
    processed_file = config.get('report_generator', 'processed_file')
    station_id = config.get('report_generator', 'station_id')
    in_dir = config.get('paths', 'in_dir')
    ftp_results_dir = config.get('paths', 'ftp_results_dir')

    if logger is None:
        logger = setup_logging(ftp_results_dir)

    logger.info("=" * 60)
    logger.info("CADIP Report Generator — Starting")
    logger.info("=" * 60)

    if dry_run:
        logger.info("*** DRY-RUN MODE ***")

    # Load processed folders (purges entries > 3 days)
    processed = load_processed_folders(processed_file, logger)
    logger.debug(
        "Loaded {} processed folder entries".format(len(processed))
    )

    # Scan for new acquisition folders
    new_folders = scan_acquisition_folders(reports_source, processed, logger)

    if not new_folders:
        logger.info("No new acquisition folders found.")
        save_processed_folders(processed_file, processed)
        return 0

    logger.info("Found {} new folder(s)".format(len(new_folders)))

    generated_count = 0
    for folder_path in new_folders:
        folder_name = os.path.basename(folder_path)

        if dry_run:
            logger.info("  Would process: {}".format(folder_name))
            continue

        eof_filename = process_folder(
            folder_path, in_dir, station_id, logger
        )
        if eof_filename:
            mark_as_processed(processed, folder_name)
            generated_count += 1
        else:
            logger.warning(
                "  Skipping {} due to errors".format(folder_name)
            )

    # Save updated processed folders list
    save_processed_folders(processed_file, processed)

    logger.info(
        "Report Generator — Finished ({} file(s) generated)".format(
            generated_count
        )
    )
    return generated_count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CADIP Report Generator — parse acquisition XMLs "
                    "and generate REP_PASS EOF files"
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "cadip.ini"),
        help="Path to cadip.ini config file (default: same dir as script)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan folders but do not generate EOF files"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    run(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
