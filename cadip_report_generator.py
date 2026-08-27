#!/usr/bin/env python3
"""
CADIP Report Generator — Scans acquisition folders, parses XML reports,
and generates REP_PASS EOF files for upload to inta-ddp.

Pipeline:
  /disk3/reports/SXY_xxxxxxxxx/
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
from datetime import datetime, timezone


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
# Pass index (formerly the flat processed-folders list)
# ---------------------------------------------------------------------------
#
# A pass does not always land in one folder. When the acquiring DFEP cannot
# deliver, it leaves behind a folder holding only the demodulation output, and
# the later retransfer writes one further folder *per channel* holding only the
# DSIB. Neither half can produce a report on its own, so the index records the
# satellite and orbit of every folder seen and lets a retransfer find the
# acquisition it belongs to without rescanning and re-parsing the directory.
#
# Line format:  timestamp|folder|satellite|orbit|kind|state|eof
# Legacy two-field lines (timestamp|folder) are still read, as done/legacy.

KIND_COMPLETE = 'complete'          # reconstructs + DSIB together — nominal
KIND_ACQUISITION = 'acquisition'    # reconstructs only — demodulated, not distributed
KIND_DISTRIBUTION = 'distribution'  # DSIB only — distributed, not demodulated
KIND_UNKNOWN = 'unknown'            # nothing recognisable yet, probably mid-write
KIND_LEGACY = 'legacy'              # carried over from the old file format

STATE_DONE = 'done'                 # nothing further to do with this folder
STATE_PENDING = 'pending'           # revisit on later runs


def load_index(processed_file, reports_source, logger):
    """
    Load the pass index.

    A row is dropped only when its folder no longer exists under
    reports_source, so the index lives exactly as long as the data it points
    at. There is deliberately no age-based purge: a retransfer may follow its
    acquisition by minutes, hours or days, and the acquisition row has to
    still be there to supply the frame statistics.

    Returns {folder_name: row_dict}.
    """
    rows = {}
    if not os.path.isfile(processed_file):
        return rows

    # Only purge while the source directory is actually readable. If the mount
    # is missing, keeping the index intact is far better than emptying it and
    # reprocessing every folder on the next run.
    source_readable = os.path.isdir(reports_source)
    dropped = 0

    with open(processed_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or '|' not in line:
                continue
            parts = line.split('|')
            if len(parts) < 2:
                continue
            row = {
                'timestamp': parts[0],
                'folder': parts[1],
                'satellite': parts[2] if len(parts) > 2 else '',
                'orbit': parts[3] if len(parts) > 3 else '',
                'kind': parts[4] if len(parts) > 4 else KIND_LEGACY,
                'state': parts[5] if len(parts) > 5 else STATE_DONE,
                'eof': parts[6] if len(parts) > 6 else '',
            }
            if source_readable and not os.path.isdir(
                    os.path.join(reports_source, row['folder'])):
                dropped += 1
                continue
            rows[row['folder']] = row

    if dropped:
        logger.debug(
            "Dropped {} index row(s) whose folder is gone".format(dropped)
        )
    return rows


def save_index(processed_file, rows):
    """Write the pass index back to disk."""
    with open(processed_file, 'w') as f:
        for folder_name in sorted(rows):
            r = rows[folder_name]
            f.write("{}|{}|{}|{}|{}|{}|{}\n".format(
                r['timestamp'], r['folder'], r['satellite'], r['orbit'],
                r['kind'], r['state'], r['eof'],
            ))


def index_row(rows, folder_name, satellite='', orbit='',
              kind=KIND_UNKNOWN, state=STATE_PENDING, eof=''):
    """Insert or update a row, refreshing its timestamp."""
    rows[folder_name] = {
        'timestamp': datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        'folder': folder_name,
        'satellite': satellite or '',
        'orbit': str(orbit or ''),
        'kind': kind,
        'state': state,
        'eof': eof or '',
    }


def find_acquisition_folder(rows, satellite, orbit, reports_source):
    """
    Find the indexed folder holding the demodulation output for this pass.

    Returns the folder name, or None. Rows are only kept while their folder
    exists, but the reconstruct XMLs are checked again here because the folder
    may have been pruned between runs.
    """
    for folder_name in sorted(rows):
        row = rows[folder_name]
        if row['satellite'] != satellite or row['orbit'] != str(orbit):
            continue
        if row['kind'] not in (KIND_ACQUISITION, KIND_COMPLETE):
            continue
        found = classify_folder(os.path.join(reports_source, folder_name))
        if found['ch1'] and found['ch2']:
            return folder_name
    return None


# ---------------------------------------------------------------------------
# Folder scanning and classification
# ---------------------------------------------------------------------------

# Pattern: SXY_ followed by 9 alphanumeric characters
FOLDER_PATTERN = re.compile(r'^S[12][A-D]_[A-Za-z0-9]{9}$')

CH1_RECONSTRUCT = 'reconstruct_xband1_ch1_VCDU1.xml'
CH2_RECONSTRUCT = 'reconstruct_xband2_ch2_VCDU1.xml'


def scan_acquisition_folders(reports_source, rows, logger):
    """
    Return the folders worth looking at this run: every SXY_xxxxxxxxx folder
    not yet indexed, plus indexed ones still marked pending.

    There is no modification-time cutoff. Index rows now disappear only when
    their folder does, so an old folder can never be re-detected as new —
    which is the duplicate-report problem the cutoff was working around.
    """
    if not os.path.isdir(reports_source):
        logger.error(
            "Reports source directory not found: {}".format(reports_source)
        )
        return []

    folders = []
    for entry in os.scandir(reports_source):
        if not entry.is_dir():
            continue
        if not FOLDER_PATTERN.match(entry.name):
            continue
        row = rows.get(entry.name)
        if row is not None and row['state'] == STATE_DONE:
            continue
        folders.append(entry.path)

    folders.sort()
    return folders


def classify_folder(folder_path):
    """
    Describe a folder by what it holds. See the pass-index note above for why
    the three partial shapes exist.
    """
    ch1 = os.path.join(folder_path, CH1_RECONSTRUCT)
    ch2 = os.path.join(folder_path, CH2_RECONSTRUCT)
    has_ch1 = os.path.isfile(ch1)
    has_ch2 = os.path.isfile(ch2)
    dsibs = sorted(glob.glob(os.path.join(folder_path, 'DCS_0*_DSIB.xml')))

    if has_ch1 and has_ch2 and dsibs:
        kind = KIND_COMPLETE
    elif has_ch1 or has_ch2:
        kind = KIND_ACQUISITION
    elif dsibs:
        kind = KIND_DISTRIBUTION
    else:
        kind = KIND_UNKNOWN

    return {
        'kind': kind,
        'ch1': ch1 if has_ch1 else None,
        'ch2': ch2 if has_ch2 else None,
        'dsibs': dsibs,
    }


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
    all_summaries = root.findall('.//Summary')
    summary_elem = all_summaries[-1] if all_summaries else None
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
        logger.warning(
            "No Status VCID=63 found in {} — defaulting to zero idle frames".format(filepath)
        )
        result['status_63'] = {'NumFrames': 0, 'RsUncorrectable': 0, 'RsCorrectable': 0}
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

def read_reconstruct_identity(filepath, logger):
    """Satellite name and orbit from a reconstruct XML (<Name>, <Orbit>)."""
    try:
        root = ET.parse(filepath).getroot()
    except ET.ParseError as e:
        logger.error("Failed to parse {}: {}".format(filepath, e))
        return None, None
    return root.findtext('Name'), root.findtext('Orbit')


def read_dsib_identity(filepath, logger):
    """
    Satellite, orbit and DFEP unit from a DSIB filename.

    Two layouts are in use and both occur in the field:
        DCS_04_S2B_20260410112241_47496_ch1_DSIB.xml   time and orbit separate
        DCS_04_S1D_20260826081034004294_ch1_DSIB.xml   fused, orbit = last 6

    <session_id> inside the file carries the fused form too, but it is empty in
    some reports, so the filename is the source of truth here.

    Returns (satellite, orbit, unit) or (None, None, None).
    """
    name = os.path.basename(filepath)
    parts = name.split('_')
    if len(parts) < 5 or not parts[1].isdigit():
        logger.error("Unrecognised DSIB filename: {}".format(name))
        return None, None, None

    unit_number = str(int(parts[1]))
    satellite = parts[2]
    middle = parts[3:-2]      # fields between the satellite and _chN_DSIB.xml

    orbit = None
    if len(middle) == 1 and len(middle[0]) == 20 and middle[0].isdigit():
        orbit = str(int(middle[0][14:]))
    elif len(middle) >= 2 and middle[-1].isdigit():
        orbit = str(int(middle[-1]))

    if orbit is None:
        logger.error(
            "Cannot derive orbit from DSIB filename: {}".format(name)
        )
        return None, None, None

    return satellite, orbit, unit_number


def build_metadata(satellite, unit_number, dsib_path, ch1_path, ch2_path,
                   logger):
    """
    Assemble the three inputs a REP_PASS needs, wherever they happen to live:
    frame statistics per channel from the reconstruct XMLs, pass window and
    DFEP unit from the DSIB. For a nominal pass all three come from one
    folder; for a retransfer the DSIB comes from a different folder than the
    reconstruct XMLs.

    Returns dict with all metadata, or None on error.
    """
    ch1_data = parse_reconstruct_xml(ch1_path, logger)
    ch2_data = parse_reconstruct_xml(ch2_path, logger)
    if not ch1_data or not ch2_data:
        return None

    dcs_data = parse_dcs_xml(dsib_path, logger)
    if not dcs_data:
        return None

    return {
        'mission': satellite[1:3],
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

def generate_report(metadata, in_dir, station_id, logger):
    """Build the REP_PASS XML and write it to IN/. Returns the EOF filename."""
    root_elem = generate_eof_xml(metadata)
    eof_filename = build_eof_filename(metadata, station_id)
    os.makedirs(in_dir, exist_ok=True)
    write_eof_file(root_elem, os.path.join(in_dir, eof_filename), logger)
    return eof_filename


def _announce(rows, folder_name, kind, message, logger):
    """
    Log a folder's state only when it is new or has changed.

    Folders that cannot yet produce a report stay pending indefinitely — by
    design, since a retransfer has no deadline — so an unconditional message
    here would repeat on every run of the two-minute cron.
    """
    previous = rows.get(folder_name)
    if previous is None or previous['kind'] != kind:
        logger.info(message)


def _handle_acquisition_side(folder_path, info, rows, in_dir, station_id,
                             dry_run, logger):
    """
    Handle a folder that carries demodulation output. Returns 1 if a report
    was generated, 0 otherwise.
    """
    folder_name = os.path.basename(folder_path)

    satellite = orbit = None
    for path in (info['ch1'], info['ch2']):
        if path:
            satellite, orbit = read_reconstruct_identity(path, logger)
            if satellite and orbit:
                break

    if not satellite or not orbit:
        logger.error("Cannot identify the pass in {}".format(folder_name))
        index_row(rows, folder_name, kind=info['kind'], state=STATE_PENDING)
        return 0

    if info['kind'] == KIND_ACQUISITION:
        # Demodulated but not distributed. Nothing to report yet: either a DSIB
        # lands in this folder later (nominal), or a retransfer folder appears
        # and borrows these frame statistics. Pending serves both.
        _announce(rows, folder_name, KIND_ACQUISITION,
                  "Indexed acquisition {} ({} orbit {}) — no DSIB yet".format(
                      folder_name, satellite, orbit), logger)
        index_row(rows, folder_name, satellite, orbit,
                  KIND_ACQUISITION, STATE_PENDING)
        return 0

    # KIND_COMPLETE — the nominal case, everything in one folder.
    dsib_path = info['dsibs'][0]
    _, _, unit_number = read_dsib_identity(dsib_path, logger)
    if not unit_number:
        index_row(rows, folder_name, satellite, orbit,
                  KIND_COMPLETE, STATE_PENDING)
        return 0

    if dry_run:
        logger.info("  Would generate report for {} orbit {} ({})".format(
            satellite, orbit, folder_name))
        return 0

    logger.info("Processing: {}".format(folder_name))
    metadata = build_metadata(satellite, unit_number, dsib_path,
                              info['ch1'], info['ch2'], logger)
    if not metadata:
        logger.error("  Failed to extract metadata from {}".format(folder_name))
        index_row(rows, folder_name, satellite, orbit,
                  KIND_COMPLETE, STATE_PENDING)
        return 0

    eof_filename = generate_report(metadata, in_dir, station_id, logger)
    index_row(rows, folder_name, satellite, orbit,
              KIND_COMPLETE, STATE_DONE, eof_filename)
    return 1


def _handle_retransfers(classified, rows, reports_source, in_dir, station_id,
                        dry_run, logger):
    """
    Resolve retransfer folders against the acquisition they belong to.

    A retransfer writes one folder per channel and both carry the same session,
    pass window and unit — only data_size differs — so the pair yields a single
    report. Grouping is per run: a genuinely new retransfer of the same pass
    later on arrives in new folders and correctly produces a fresh report.
    """
    groups = {}
    for folder_path, info in classified:
        if info['kind'] != KIND_DISTRIBUTION:
            continue
        folder_name = os.path.basename(folder_path)
        dsib_path = info['dsibs'][0]
        satellite, orbit, unit_number = read_dsib_identity(dsib_path, logger)
        if not satellite:
            index_row(rows, folder_name, kind=KIND_DISTRIBUTION,
                      state=STATE_PENDING)
            continue
        groups.setdefault((satellite, orbit, unit_number), []).append(
            (folder_name, dsib_path)
        )

    generated = 0
    for (satellite, orbit, unit_number), members in sorted(groups.items()):
        folder_names = sorted(m[0] for m in members)
        dsib_path = members[0][1]
        joined = ", ".join(folder_names)

        acq_folder = find_acquisition_folder(
            rows, satellite, orbit, reports_source
        )
        if acq_folder is None:
            # Terminal: without the demodulation output there is no report to
            # build, and no amount of waiting will produce one.
            logger.error(
                "Retransfer {} orbit {} ({}): no acquisition folder with "
                "demodulation output for this pass — cannot build a "
                "report".format(satellite, orbit, joined)
            )
            for folder_name in folder_names:
                index_row(rows, folder_name, satellite, orbit,
                          KIND_DISTRIBUTION, STATE_DONE)
            continue

        if dry_run:
            logger.info(
                "  Would generate retransfer report for {} orbit {} "
                "from {} + {}".format(satellite, orbit, acq_folder, joined)
            )
            continue

        acq_info = classify_folder(os.path.join(reports_source, acq_folder))
        metadata = build_metadata(satellite, unit_number, dsib_path,
                                  acq_info['ch1'], acq_info['ch2'], logger)
        if not metadata:
            logger.error(
                "Retransfer {} orbit {} ({}): failed to build metadata "
                "from {}".format(satellite, orbit, joined, acq_folder)
            )
            for folder_name in folder_names:
                index_row(rows, folder_name, satellite, orbit,
                          KIND_DISTRIBUTION, STATE_PENDING)
            continue

        logger.info(
            "Retransfer {} orbit {}: frame statistics from {}, pass window "
            "from {}".format(satellite, orbit, acq_folder, joined)
        )
        eof_filename = generate_report(metadata, in_dir, station_id, logger)
        for folder_name in folder_names:
            index_row(rows, folder_name, satellite, orbit,
                      KIND_DISTRIBUTION, STATE_DONE, eof_filename)
        generated += 1

    return generated


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

    rows = load_index(processed_file, reports_source, logger)
    logger.debug("Loaded {} index row(s)".format(len(rows)))

    folders = scan_acquisition_folders(reports_source, rows, logger)
    if not folders:
        logger.info("No folders to examine.")
        if not dry_run:
            save_index(processed_file, rows)
        return 0

    logger.info("Examining {} folder(s)".format(len(folders)))

    # Classify everything up front. A retransfer that turns up in the same scan
    # as its acquisition must still find it, so every acquisition is indexed
    # before any retransfer is resolved.
    classified = [(path, classify_folder(path)) for path in folders]

    generated_count = 0

    for folder_path, info in classified:
        folder_name = os.path.basename(folder_path)
        if info['kind'] == KIND_DISTRIBUTION:
            continue
        if info['kind'] == KIND_UNKNOWN:
            # Most likely still being written — stay quiet and look again later.
            logger.debug(
                "Nothing recognisable yet in {}".format(folder_name)
            )
            index_row(rows, folder_name, kind=KIND_UNKNOWN,
                      state=STATE_PENDING)
            continue
        generated_count += _handle_acquisition_side(
            folder_path, info, rows, in_dir, station_id, dry_run, logger
        )

    generated_count += _handle_retransfers(
        classified, rows, reports_source, in_dir, station_id, dry_run, logger
    )

    if not dry_run:
        save_index(processed_file, rows)

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
