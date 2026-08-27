# CADIP Report Pipeline

Automated pipeline for generating and distributing satellite acquisition reports to the INTA Data Delivery Point (DDP).

## Overview

When a satellite pass is acquired at the ground station, the acquisition system generates a set of XML reports and telemetry files in a dedicated folder. This pipeline:

1. **Detects** new acquisition folders as they appear
2. **Parses** the telemetry XML files to extract frame statistics per channel
3. **Generates** a standardised REP_PASS report (EOF file) summarising data quality
4. **Transfers** the report to the remote DDP server (`inta-ddp.inta.es`) via FTP
5. **Verifies** each transfer and handles failures automatically

The pipeline runs every 2 minutes via cron and requires **no external dependencies** — only Python 3.6+ standard library.

---

## Architecture

```
/disk3/reports/SXY_xxxxxxxx/       ← acquisition folder
    ├── reconstruct_xband1_ch1_VCDU1.xml         (channel 1 telemetry)
    ├── reconstruct_xband2_ch2_VCDU1.xml         (channel 2 telemetry)
    └── DCS_0n_Sxy_..._DSIB.xml                  (session timing info)
                     │
            cadip_report_generator.py
                     │
                     ▼
    IN/  Sxy_OPER_REP_PASS_n_MPS__..._V..._....EOF   ← generated report
                     │
               cadip_upload.py
              ┌──────┴──────┐
              ▼              ▼
    MD5/  (file_<hash>)  →  FTP upload to inta-ddp
              │              (WebReports/DFEP{n}/)
              ▼
    Sent_DDP/  (archived after successful transfer)
```

### Retry and Error Recovery

```
Remote *_BAD files  →  BAD_From_DDP/  →  original requeued from Sent_DDP/  →  MD5/  →  retry upload
```

If the DDP server detects a problem with a received file, it creates a `*_BAD` marker. On the next run, the pipeline downloads these markers, moves the corresponding original file back to `MD5/` for automatic re-upload.

---

## Files

| File | Description |
|------|-------------|
| `cadip_main.py` | **Orchestrator** — entry point for cron. Runs report generation then upload in sequence. |
| `cadip_report_generator.py` | **Report generator** — scans acquisition folders, parses XMLs, generates REP_PASS EOF files. |
| `cadip_upload.py` | **FTP uploader** — computes MD5 checksums, uploads to DDP, verifies transfers, handles BAD files. |
| `cadip.ini` | **Configuration** — FTP credentials, all local/remote paths, station settings. |

---

## Directory Structure

```
/home/meos/bin/cadip/
├── cadip_main.py              ← cron entry point
├── cadip_report_generator.py  ← report generation logic
├── cadip_upload.py            ← FTP upload logic
├── cadip.ini                  ← configuration
├── processed_folders.txt      ← pass index: folder state + satellite/orbit (auto-generated)
├── IN/                        ← generated EOF files (pending MD5 processing)
├── MD5/                       ← files renamed with MD5 hash (pending upload)
├── Sent_DDP/                  ← successfully uploaded files (archive)
├── BAD_From_DDP/              ← BAD markers downloaded from remote
└── ftp-results/               ← timestamped log files
```

---

## Configuration (`cadip.ini`)

```ini
[ftp]
host = inta-ddp.inta.es           # DDP server hostname
user = inta-cadip                  # FTP username
password = X214_Opmas              # FTP password

[paths]
cadip_base = /home/meos/bin/cadip  # Base directory for all working folders
in_dir = %(cadip_base)s/IN         # Staging area for generated EOF files
md5_dir = %(cadip_base)s/MD5       # Files renamed with MD5, pending upload
sent_files_dir = %(cadip_base)s/Sent_DDP      # Archive of uploaded files
bad_from_ddp_dir = %(cadip_base)s/BAD_From_DDP # BAD markers from remote
ftp_results_dir = %(cadip_base)s/ftp-results   # Log files
lock_file = %(cadip_base)s/cadip_upload.lock   # Prevents overlapping runs

[report_generator]
reports_source = /disk3/reports    # Where acquisition folders appear
processed_file = /home/meos/bin/cadip/processed_folders.txt  # Pass index
station_id = MPS                                # Station identifier for filenames

[remote]
target_path_template = WebReports/DFEP{unit}/   # {unit} replaced by processing unit (1-9)
```

---

## Report Generation Details

### Input: Acquisition Folder

Each satellite pass creates a folder named `SXY_xxxxxxxx` (e.g. `S1A_AI3B9n44`) in the reports source directory, where `XY` is the satellite mission (1A, 1C, 1D, 2A, 2B, 2C, 2D).

A pass does **not** always land in a single folder. Demodulation writes the
`reconstruct_*.xml` files; distribution writes the DSIB. When the acquiring DFEP
cannot deliver to the DDP, those two steps end up in different folders:

| folder | contents | meaning |
|---|---|---|
| `SXY_xxxxxxxxx` | both `reconstruct_*.xml`, DSIB | nominal — one report, generated immediately |
| `SXY_xxxxxxxxx` | both `reconstruct_*.xml`, no DSIB | demodulated but never distributed |
| `SXY_xxxxxxxxx` | DSIB only, one per channel | a retransfer of that pass |

A retransfer therefore carries the pass window but no frame statistics, and the
acquisition it belongs to carries frame statistics but no pass window. The
generator joins them via the pass index (below) and emits one report covering
both channels — which is what the DDP application expects, since it reads
`data_C1` and `data_C2` from a single matched file.

### XML Parsing

The report generator extracts data from two types of XML files:

**`reconstruct_xbandN_chN_VCDU1.xml`** (one per channel):
- `<Summary>` section → total frame counts across all VCIDs
- `<Status VCID="63">` section → fill/idle frame counts (non-meaningful data)
- **Data frames** = Summary totals − VCID 63 counts (gives actual useful data frames)

**`DCS_0n_Sxy_..._DSIB.xml`** (session information):
- `<time_start>` and `<time_stop>` → acquisition time window
- `n` in filename → DFEP processing unit number

### Output: REP_PASS EOF File

```xml
<?xml version='1.0' encoding='UTF-8'?>
<REP_PASS>
  <channel_1>
    <total_frames>
      <NumFrames>9294873</NumFrames>
      <RsUncorrectable>8</RsUncorrectable>
      <RsCorrectable>5</RsCorrectable>
    </total_frames>
    <data_frames>
      <NumFrames>2473438</NumFrames>
      <RsUncorrectable>6</RsUncorrectable>
      <RsCorrectable>2</RsCorrectable>
    </data_frames>
  </channel_1>
  <channel_2>
    <!-- same structure as channel_1 -->
  </channel_2>
</REP_PASS>
```

### EOF Filename Convention

```
Sxy_OPER_REP_PASS_n_SID__YYYYMMDDTHHMMSS_VYYYYMMDDTHHMMSS_YYYYMMDDTHHMMSS.EOF
│                 │  │    │                │                 │
│                 │  │    │                │                 └── acquisition stop time
│                 │  │    │                └── acquisition start time
│                 │  │    └── file generation time (UTC)
│                 │  └── station identifier (e.g. MPS)
│                 └── DFEP processing unit (1-9)
└── satellite mission (e.g. 1A, 2B)
```

Example: `S1A_OPER_REP_PASS_4_MPS__20260316T095516_V20260316T081114_20260316T082016.EOF`

---

## FTP Upload Details

### MD5 Integrity

Before upload, each file is renamed with its MD5 hash appended:
```
S1A_OPER_REP_PASS_4_MPS__2026..._V2026..._2026....EOF
  → S1A_OPER_REP_PASS_4_MPS__2026..._V2026..._2026....EOF_5fea4d1d4f621acd741c80ccebc77adb
```

This allows the DDP server to verify file integrity upon receipt.

### Dynamic Remote Path

The upload destination is determined per file from the processing unit number in the filename. Unit `4` → `WebReports/DFEP4/`, unit `5` → `WebReports/DFEP5/`, etc.

### Transfer Verification

Each upload is verified by checking for the FTP `226 Transfer complete` response:
- **Success** → file moved to `Sent_DDP/`
- **Failure** → file stays in `MD5/` for automatic retry on the next run

### BAD File Handling

The DDP server creates `*_BAD` marker files for problematic transfers. The pipeline:
1. Scans all operational DFEP directories for `*_BAD` files
2. Downloads them to `BAD_From_DDP/`
3. Finds the corresponding original in `Sent_DDP/`
4. Moves it back to `MD5/` for re-upload
5. Deletes the BAD marker

---

## Concurrency Protection

A PID-based lock file (`cadip_upload.lock`) prevents overlapping runs when cron triggers a new execution while a previous one is still active:

- If no lock exists → create it with current PID, proceed
- If lock exists and PID is alive → exit (previous run still active)
- If lock exists but PID is dead → stale lock, remove it and proceed

---

## Incomplete Acquisition Handling

Acquisition folders are created when a pass starts, but XML files only appear after the acquisition completes (several minutes later). The pipeline handles this gracefully:

- If required files are missing, the folder is left **pending** and looked at again on the next run
- A folder that cannot yet produce a report is reported **once**, not on every run of the two-minute cron
- Once all files appear, processing succeeds and the folder is marked **done**

### The pass index

`processed_folders.txt` is a state table, not just a list of names:

```
timestamp|folder|satellite|orbit|kind|state|eof
2026-08-26T08:19:41Z|S1D_IX6B9n441|S1D|4294|acquisition|pending|
2026-08-26T08:37:12Z|S1D_G17B9n441|S1D|4294|distribution|done|S1D_OPER_REP_PASS_4_MPS__...EOF
```

Recording satellite and orbit is what lets a retransfer find its acquisition
directly, without rescanning and re-parsing the whole directory. Two-field
lines from the previous format are still read, as `legacy`/`done`.

Rows are dropped **when their folder disappears from disk**, not on a timer.
There is deliberately no age limit: a retransfer may follow its acquisition by
minutes, hours or days, and the acquisition row has to still be there to supply
the frame statistics. This also removes the need for the old modification-time
cutoff in the scanner — entries can no longer expire out from under a folder
that still exists, so a stale folder cannot be re-detected as new.

The one terminal failure is a retransfer whose acquisition folder is no longer
on disk. Nothing can be built from it, so it is logged once as an error and
marked done. That puts a real ceiling on how late a retransfer can arrive: the
retention of `reports_source` itself.

---

## Deployment

### Prerequisites
- Python 3.6 or later
- FTP access to `inta-ddp.inta.es`

### Installation

```bash
# Copy files to the server
cp cadip_main.py cadip_report_generator.py cadip_upload.py cadip.ini /home/meos/bin/cadip/

# Create working directories
cd /home/meos/bin/cadip
mkdir -p IN MD5 Sent_DDP BAD_From_DDP ftp-results

# Edit configuration
vi cadip.ini
```

### Testing

```bash
# Dry-run: scan and process without FTP
python3 cadip_main.py --dry-run

# Test report generator only
python3 cadip_report_generator.py --dry-run

# Test uploader only
python3 cadip_upload.py --dry-run
```

### Cron Setup

```bash
# Run every 2 minutes
*/2 * * * * /usr/bin/python3 /home/meos/bin/cadip/cadip_main.py
```

### Logs

All logs are written to `ftp-results/` with timestamped filenames:
- `report_gen_20YYMMDDTHHMMSS.log` — report generator activity
- `cadip_20YYMMDDTHHMMSS.log` — upload activity

---

## Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| "Another instance is running" | Previous run still active or crashed | Check if process exists; if not, delete `cadip_upload.lock` |
| "Missing reconstruct XML files" | Acquisition still in progress | Wait — will auto-retry on next run |
| "Upload FAILED: 550 No such directory" | Remote DFEP directory missing | Verify `target_path_template` in `cadip.ini` and check with DDP admin |
| "Cannot determine DFEP unit from filename" | Non-EOF file in MD5/ | Manually remove or move the unexpected file |
| Files stuck in MD5/ | Repeated FTP failures | Check FTP connectivity, credentials, and logs |
