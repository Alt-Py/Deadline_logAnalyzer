"""Diagnose Deadline render-job reports for crash-risk patterns.

Two modes:
  1. Folder of .txt exports:   python deadline_cleaner.py <log_dir> -o report
  2. Live Deadline Repository: python deadline_cleaner.py --repo \\\\<srv>\\DeadlineRepository10\\jobs --since 7d
  3. GUI (default when launched with no args, or via --gui)

Outputs (always):
  - report.html  : per-job risk cards, nested by group → user → job
  - report.csv   : flat table for spreadsheet triage
  - per_student.md : supervisor-facing rollup, group → user → jobs
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

# --- Tunable thresholds --------------------------------------------------

AOV_LIMIT = 20                       # flag if AOV count per render product > this
FRAME_TIME_LIMIT_SEC = 90 * 60       # 1h30 hard ceiling per frame
RAM_HEADROOM_GIB = 1.0               # flag if peak_ram >= (slave_total - 1 GiB)
SLAVE_TIERS_GIB = (16, 32, 64)       # nominal RAM tiers in the fleet
MEMORY_GROWTH_FLAG_GIB = 5.0         # Karma mem climb during render to flag
STUCK_PROGRESS_AFTER_MIN = 30        # if < 5% progress this long in, flag
STUCK_PROGRESS_PCT = 5.0

WARN_SAMPLE_COUNT = 5                # how many warning lines to surface in the report

KARMA_RENDERER = "BRAY_HdKarma"
RENDERMAN_RENDERER = "HdPrmanLoaderRendererPlugin"

# Native mipmapped texture format per renderer. Anything else in the OIIO
# texture stats block means the student didn't pre-convert their textures.
NATIVE_TEXTURE_EXT = {KARMA_RENDERER: "rat", RENDERMAN_RENDERER: "tex"}

DATA_PFE_DRIVE = "V:"   # output here = wrong (license/project server)
DATA_FRM_DRIVE = "W:"   # output here = correct (render output server)

# Known student-group codes. Anything else gets bucketed under "OTHER".
KNOWN_GROUPS = ("HYT", "JRN", "VRT", "CNB", "PMF", "COR", "GTE")
OTHER_GROUP = "OTHER"
GROUP_BUCKETS = (*KNOWN_GROUPS, OTHER_GROUP)

# Default Deadline Repository task-reports share. The `jobs/` folder under
# the repo root holds *submission payloads* (.py wrappers + .json settings) —
# not task reports. The actual reports live under `reports/jobs/` and are
# stored as bzip2-compressed text, sharded by job-id prefix:
#   <repo>/reports/jobs/<hh>/<h>/<jobid>/<reportid>.bz2
DEFAULT_REPO_PATH = r"YOUR/REPO"

# Job-name fragments to drop from the diagnostic. These are tasks that aren't
# the Houdini renders we want to triage:
#   _cleanup → Prism's post-render USD cleanup companion jobs
#   _nuke    → Nuke comp jobs (different plugin, different log shape)
EXCLUDED_JOB_NAME_FRAGMENTS = ("_cleanup", "_nuke", "nuke_")

# Deadline job IDs are MongoDB ObjectIds — 24 hex chars.
JOB_ID_RE = re.compile(r"^[0-9a-f]{24}$")

# Number of parallel SMB-read workers. SMB latency dominates; the share
# tolerates many concurrent sessions, so ~16 threads gives close to peak
# throughput on both the directory walk and the file reads.
DEFAULT_WORKERS = 16

SEV_INFO, SEV_LOW, SEV_MED, SEV_HIGH, SEV_CRIT = "info", "low", "medium", "high", "critical"
SEV_ORDER = {SEV_INFO: 0, SEV_LOW: 1, SEV_MED: 2, SEV_HIGH: 3, SEV_CRIT: 4}

# --- Regex catalogue -----------------------------------------------------

LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):")
HUSK_RENDERER_RE = re.compile(r"--renderer['\"]?,?\s*['\"]?([A-Za-z_][A-Za-z0-9_]+)")
HUSK_OUTPUT_RE = re.compile(r"--output['\"]?,?\s*['\"]?([A-Z]:/[^'\"\s,]+)")
HUSK_INPUT_USD_RE = re.compile(r"([A-Z]:/[^'\"\s,]+\.usdc?)\b", re.IGNORECASE)
JOB_LOADED_RE = re.compile(r"Loaded job:\s*(.+?)\s*\(([0-9a-f]+)\)")
JOB_USER_RE = re.compile(r"^Job User:\s*(\S+)", re.MULTILINE)
RUNNING_AS_RE = re.compile(r"Running as user:\s*(\S+)")
WORKER_NAME_RE = re.compile(r"^Worker Name:\s*(\S+)", re.MULTILINE)
ELAPSED_RE = re.compile(r"^Elapsed Time:\s*(\d+):(\d+):(\d+):(\d+)", re.MULTILINE)
WRAPPER_PY_RE = re.compile(r"([A-Za-z0-9_.\-]+)_\d{8,}\.py")
PEAK_RAM_DETAILS_RE = re.compile(r"^Peak RAM Usage:\s*(\d+)", re.MULTILINE)
AVG_RAM_DETAILS_RE = re.compile(r"^Average RAM Usage:\s*(\d+)", re.MULTILINE)
HUSK_PEAK_MEM_RE = re.compile(r"Peak Memory Usage:\s*([\d.]+)\s*GiB")
SUBMIT_DATE_RE = re.compile(r"^Job Submit Date:\s*(.+)$", re.MULTILINE)
COMPLETION_DATE_RE = re.compile(r"^Date:\s*(.+)$", re.MULTILINE)
WORKER_MEM_RE = re.compile(r"Memory Usage:\s*([\d.]+)\s*GB\s*/\s*([\d.]+)\s*GB")
WORKER_CPUS_RE = re.compile(r"^CPUs:\s*(\d+)", re.MULTILINE)
WORKER_OS_RE = re.compile(r"^Operating System:\s*(.+)$", re.MULTILINE)
WORKER_GPU_RE = re.compile(r"^Video Card:\s*(.+)$", re.MULTILINE)
FRAME_RE = re.compile(r"Plugin rendering frame\(s\):\s*(\S+)")
KARMA_PROGRESS_RE = re.compile(
    r"\[\d+:\d+:\d+\]\s+([\d.]+)%\s+Lap=\s*([\d:.]+)\s+Left=\s*([\d:.]+)\s+"
    r"Mem=\s*([\d.]+)\s*GiB\s+Peak=\s*([\d.]+)\s*GiB"
)
AOV_NAME_RE = re.compile(r'"driver:parameters:aov:name"\s*:\s*"([^"]+)"')
DRIVE_MAP_RE = re.compile(r"Successfully mapped (\w:) to (\S+)")
TASK_TIMEOUT_RE = re.compile(r"Task timed out -- canceling")
USD_LOAD_FAIL_RE = re.compile(r"Unable to load USD file '([^']+)'")
EXIT_CODE_RE = re.compile(r"Process exit code:\s*(\d+)")
SUCCESS_RE = re.compile(r"task completed successfully")
HUSK_PATH_ERROR_RE = re.compile(r"Husk render executable is not defined")
GROUP_FROM_PATH_RE = re.compile(r"/L\d+_\d+/([A-Z]{3,4})/", re.IGNORECASE)

# OIIO texture stats: locate the "Image file statistics:" block; each row ends
# in the file path and optionally " UNTILED".
OIIO_BLOCK_RE = re.compile(
    r"OpenImageIO ImageCache statistics.+?Image file statistics:\s*\n(.+?)(?=\n[\s\S]{0,200}?OpenImageIO|\Z)",
    re.DOTALL,
)
OIIO_FILE_LINE_RE = re.compile(
    r"STDOUT:\s+\d+\s+\d+\s+\d+\s+[\d.]+\s+[\d.]*\s*[\d.]*s?\s+\S+\s+(\S+\.(rat|tex|tx|exr|tif|tiff|jpg|jpeg|png|hdr))(\s+UNTILED)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Husk/Karma render warnings — `[HH:MM:SS] Warning: ...` or `Mesh ... missing ...`.
WARNING_RE = re.compile(
    r"STDOUT:\s+\[\d+:\d+:\d+\]\s+(Warning:[^\n\r]+|"
    r"Mesh\s+\S+\s+missing[^\n\r]+|"
    r"Error:[^\n\r]+)"
)
# Renderman R/S diagnostic codes (e.g. "R56009 {SEVERE} ...").
RENDERMAN_CODE_RE = re.compile(r"STDOUT:\s+([RS]\d{4,5}\s*\{[^}]+\}[^\n\r]+)")


@dataclass
class Flag:
    code: str
    severity: str
    message: str


@dataclass
class Report:
    source_file: str
    job_name: str = ""
    job_id: str = ""
    job_user: str = ""
    machine_user: str = ""
    group_code: str = ""
    renderer: str = ""
    input_usd: str = ""
    output_path: str = ""
    output_drive: str = ""
    frame: str = ""
    worker_name: str = ""
    worker_total_gib: float = 0.0
    worker_tier_gib: int = 0
    worker_cpus: int = 0
    worker_gpu: str = ""
    worker_os: str = ""
    peak_ram_gib: float = 0.0
    peak_ram_source: str = ""   # "husk" or "deadline"
    avg_ram_gib: float = 0.0
    elapsed_sec: int = 0
    elapsed_source: str = ""    # "details" or "timestamps"
    submit_date: str = ""
    completion_date: str = ""
    aov_count: int = 0
    aov_names: list[str] = field(default_factory=list)
    karma_mem_curve: list[tuple[float, float]] = field(default_factory=list)  # (lap_sec, mem_gib)
    karma_progress_curve: list[tuple[float, float]] = field(default_factory=list)  # (lap_sec, percent)
    drive_map: dict[str, str] = field(default_factory=dict)
    outcome: str = "unknown"    # per-task: success | failed | timeout | usd_load_failed | env_config_error
    job_status: str = ""        # Deadline job-level status (from deadlinecommand)
    plugin: str = ""            # Deadline PluginName (from deadlinecommand)
    # Texture / mipmap analysis. Source is "oiio" (from log stats) or "usd"
    # (scanned the USD stage via --inspect-usd). USD source is the only path
    # for Renderman renders since Renderman doesn't print OIIO stats.
    texture_data_found: bool = False
    texture_source: str = ""         # "oiio" | "usd" | ""
    texture_total: int = 0
    texture_native: int = 0          # extension matches renderer's native mipmap format
    texture_tiled_nonnative: int = 0 # tiled .exr/.tif — usable as mipmap but not pipeline-spec
    texture_untiled: int = 0         # UNTILED (OIIO) or raw .jpg/.png/.hdr (USD source)
    texture_ext_counts: dict[str, int] = field(default_factory=dict)
    texture_disk_bytes: int = 0      # sum of stat-able texture file sizes (USD source only)
    texture_unreachable: int = 0     # texture refs we could not stat from this workstation
    # Render-engine warnings (low-severity diagnostics)
    warnings: list[str] = field(default_factory=list)
    # USD dependency stats (only filled when --inspect-usd is used)
    usd_dep_count: int = 0
    usd_dep_bytes: int = 0
    usd_inspect_status: str = ""     # "ok" | "skipped" | "no_pxr" | "open_failed" | "unreachable"
    flags: list[Flag] = field(default_factory=list)
    severity: str = SEV_INFO


# --- Parser ---------------------------------------------------------------

def _hms_to_sec(s: str) -> float:
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(s)


def _classify_slave_tier(total_gib: float) -> int:
    if total_gib <= 0:
        return 0
    for tier in SLAVE_TIERS_GIB:
        if total_gib <= tier + 0.5:
            return tier
    return SLAVE_TIERS_GIB[-1]


def _group_from_path(path: str) -> str:
    m = GROUP_FROM_PATH_RE.search(path)
    return m.group(1).upper() if m else ""


def bucket_group(code: str) -> str:
    """Map a raw 3-letter group code into one of the known buckets or OTHER."""
    return code.upper() if code and code.upper() in KNOWN_GROUPS else OTHER_GROUP


SINCE_UNITS = {"h": 3600, "d": 86400, "w": 604800}


def parse_since(spec: str) -> timedelta | None:
    """Parse a lookback spec like '24h' / '7d' / '30d' / 'all' into a timedelta."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "all":
        return None
    m = re.fullmatch(r"(\d+)\s*([hdw])", spec)
    if not m:
        raise ValueError(f"unrecognised --since value: {spec!r} (try 24h, 7d, 30d, all)")
    return timedelta(seconds=int(m.group(1)) * SINCE_UNITS[m.group(2)])


# Quick sniff to skip non-report .txt files in legacy folder mode (plugin
# aux scripts, etc). Real task reports always start with this header.
_REPORT_HEAD_RE = re.compile(rb"^={3,}\s*\r?\n\s*(Log|Error)\s*\r?\n={3,}", re.MULTILINE)


def _looks_like_report(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return bool(_REPORT_HEAD_RE.search(head))


def read_report_text(path: Path) -> str:
    """Read a report file, transparently decompressing .bz2."""
    if path.suffix.lower() == ".bz2":
        import bz2
        return bz2.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def scan_repo(repo_path: Path, since: timedelta | None,
              limit: int | None = None) -> list[Path]:
    """Return Deadline task-report files from `<repo>/<hh>/<h>/<jobid>/`.

    Deadline 10 bzip2-compresses each report and shards them two levels deep
    (the shard names are NOT the job-id prefix). We parallel-walk to collect
    every job folder, apply the `since` cutoff, sort newest-first, and — when
    `limit` is set — keep only the newest `limit` *job folders* (returning all
    reports inside them).

    Capping on jobs, not report files, is deliberate: the newest report files
    on a live farm are whatever is rendering *right now* (mostly in-progress /
    just-succeeded frames), so a report-cap buries the older failures we want
    to triage. A job-cap gives an even spread of distinct jobs across the day.

    Falls back to a flat folder of .txt files (legacy / manual-export mode).
    """
    if not repo_path.exists():
        raise FileNotFoundError(f"repository path not reachable: {repo_path}")
    cutoff = (datetime.now() - since).timestamp() if since else 0.0

    try:
        top_entries = list(repo_path.iterdir())
    except OSError as e:
        raise FileNotFoundError(f"can't list {repo_path}: {e}") from e

    # Sharded layout: <repo>/<hh>/<h>/<jobid>/<reportid>.bz2
    shard_dirs = [p for p in top_entries
                  if p.is_dir() and re.fullmatch(r"[0-9a-f]{1,2}", p.name)]
    if shard_dirs:
        from concurrent.futures import ThreadPoolExecutor

        # Step 1 — flatten to every <hh>/<h>/ shard (parallel iterdir).
        def _list_h(hh: Path) -> list[Path]:
            try:
                return [p for p in hh.iterdir() if p.is_dir()]
            except OSError:
                return []

        h_dirs: list[Path] = []
        with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
            for batch in ex.map(_list_h, shard_dirs):
                h_dirs.extend(batch)

        # Step 2 — collect (job_dir, mtime) for every job, since-filtered.
        def _list_jobs(h: Path) -> list[tuple[Path, float]]:
            out: list[tuple[Path, float]] = []
            try:
                for jd in h.iterdir():
                    if not jd.is_dir():
                        continue
                    mt = _safe_mtime(jd)
                    if mt >= cutoff:
                        out.append((jd, mt))
            except OSError:
                pass
            return out

        jobs: list[tuple[Path, float]] = []
        with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
            for batch in ex.map(_list_jobs, h_dirs):
                jobs.extend(batch)

        # Step 3 — newest-first, then cap to `limit` job folders.
        jobs.sort(key=lambda t: t[1], reverse=True)
        if limit and limit > 0:
            jobs = jobs[:limit]

        # Step 4 — collect every report file inside the kept jobs.
        def _reports(jd: Path) -> list[Path]:
            try:
                return list(jd.glob("*.bz2")) or list(jd.glob("*.txt"))
            except OSError:
                return []

        result: list[Path] = []
        with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
            for batch in ex.map(_reports, [jd for jd, _ in jobs]):
                result.extend(batch)
        return result

    # Legacy flat layout: a folder of .txt files (manual exports / tests).
    # No job grouping here, so the cap applies per-file.
    flat_files = sorted([p for p in top_entries if p.is_file() and p.suffix == ".txt"],
                        key=lambda p: _safe_mtime(p), reverse=True)
    flat_files = [p for p in flat_files if _safe_mtime(p) >= cutoff]
    if limit and limit > 0:
        flat_files = flat_files[:limit]
    return [p for p in flat_files if _looks_like_report(p)]


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def files_for_job_ids(repo_path: Path, job_ids: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Return (report files, missing IDs) for a hand-picked set of Deadline jobs.

    Deadline 10's shard layout doesn't derive `<hh>/<h>` from the job-ID prefix
    (verified: job `69fefb…` lives at `ff/a/`, not `69/f/`). We have to search.
    To stay fast we parallel-list every `<hh>/<h>/` shard and filter folder
    names against the wanted set — one pass finds all requested IDs at once
    (~4 s on the live share regardless of how many IDs you pass).
    """
    from concurrent.futures import ThreadPoolExecutor

    if not repo_path.exists():
        raise FileNotFoundError(f"repository path not reachable: {repo_path}")

    wanted: set[str] = set()
    missing: list[str] = []
    for raw in job_ids:
        jid = raw.strip().lower()
        if not jid:
            continue
        if not JOB_ID_RE.fullmatch(jid):
            missing.append(f"{raw!r} (not a valid 24-hex job ID)")
            continue
        wanted.add(jid)
    if not wanted:
        return [], missing

    # Enumerate every <hh>/<h>/ leaf shard in parallel.
    def _list_h(hh: Path) -> list[Path]:
        try:
            return [p for p in hh.iterdir() if p.is_dir()]
        except OSError:
            return []

    top = [p for p in repo_path.iterdir()
           if p.is_dir() and re.fullmatch(r"[0-9a-f]{1,2}", p.name)]
    h_dirs: list[Path] = []
    with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
        for batch in ex.map(_list_h, top):
            h_dirs.extend(batch)

    # For each shard, list job folders and pick the ones we want.
    def _scan(h: Path) -> list[Path]:
        out: list[Path] = []
        try:
            for child in h.iterdir():
                if child.is_dir() and child.name in wanted:
                    out.extend(child.glob("*.bz2"))
                    out.extend(child.glob("*.txt"))
        except OSError:
            pass
        return out

    found: list[Path] = []
    located: set[str] = set()
    with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
        for batch in ex.map(_scan, h_dirs):
            for fp in batch:
                found.append(fp)
                located.add(fp.parent.name)

    missing.extend(jid for jid in wanted if jid not in located)
    return found, missing


# --- Deadline database access via deadlinecommand ------------------------
#
# `deadlinecommand` is the Deadline Client's read-only query CLI. Every query
# used here (GetJobsFilter, GetJobLogReportFilenames, …) only *reads* — nothing
# modifies jobs or the Repository. It is the authoritative source for the job
# list/status, which the file-share scan can only guess at.

DEADLINE_COMMAND_CANDIDATES = (
    r"C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe",
    r"C:\Program Files (x86)\Thinkbox\Deadline10\bin\deadlinecommand.exe",
)
# Deadline job statuses (the values GetJobsFilter Status= accepts).
JOB_STATUSES = ("Active", "Pending", "Suspended", "Completed", "Failed")
DEFAULT_JOB_STATUSES = ("Failed", "Active")

_PRISM_JOB_RE = re.compile(
    r"PRISM_JOB(?:_WDISK)?(?:_LOCAL)?=[A-Za-z]:[/\\]L\d+_\d+[/\\]([A-Za-z]{2,4})",
    re.IGNORECASE,
)
# Job names are always prefixed with the group code, e.g. "GTE_sq0050-sh0270…".
_JOB_NAME_GROUP_RE = re.compile(r"^([A-Za-z]{2,4})_")


def group_from_job_name(name: str) -> str:
    """Extract the leading group code from a job name (the reliable signal)."""
    m = _JOB_NAME_GROUP_RE.match(name or "")
    return m.group(1).upper() if m else ""


@dataclass
class JobMeta:
    """Authoritative job-level facts straight from the Deadline database."""
    job_id: str
    name: str = ""
    user: str = ""
    status: str = ""        # Deadline job status: Failed / Active / Completed / …
    plugin: str = ""        # PluginName: Houdini / Nuke / Python / …
    comment: str = ""
    submit_dt: datetime | None = None
    group_code: str = ""    # derived from PRISM_JOB in the job environment
    error_reports: int = 0
    task_count: int = 0


def find_deadlinecommand() -> str | None:
    """Locate deadlinecommand.exe via DEADLINE_PATH, known install dirs, or PATH."""
    env = os.environ.get("DEADLINE_PATH", "")
    if env:
        cand = Path(env) / "deadlinecommand.exe"
        if cand.exists():
            return str(cand)
    for c in DEADLINE_COMMAND_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("deadlinecommand")


def _run_dc(dc: str, *args: str, timeout: int = 180) -> list[str]:
    """Run a deadlinecommand query and return stdout lines."""
    proc = subprocess.run([dc, *args], capture_output=True, text=True,
                          timeout=timeout, errors="replace")
    return proc.stdout.splitlines()


def _parse_kv_records(lines: list[str]) -> list[dict[str, str]]:
    """Parse deadlinecommand `Key=Value` output into per-job records.

    GetJobsFilter prints each job as an alphabetically-ordered block of
    `Key=Value` lines with no separator. A key reappearing marks a new record.
    """
    records: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in lines:
        key, sep, val = line.partition("=")
        if not sep:
            continue
        if key in cur:
            records.append(cur)
            cur = {}
        cur[key] = val
    if cur:
        records.append(cur)
    return records


def _parse_dc_datetime(s: str) -> datetime | None:
    """Parse deadlinecommand timestamps, e.g. 'Apr 16/26  21:51:57'."""
    s = " ".join(s.split())
    for fmt in ("%b %d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%b %d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def jobmeta_from_record(rec: dict[str, str]) -> JobMeta:
    """Build a JobMeta from a parsed deadlinecommand job record."""
    jm = JobMeta(
        job_id=rec.get("JobId") or rec.get("ID", ""),
        name=rec.get("Name", ""),
        user=rec.get("UserName", ""),
        status=rec.get("JobStatus", ""),
        plugin=rec.get("PluginName", ""),
        comment=rec.get("Comment", ""),
        submit_dt=_parse_dc_datetime(rec.get("JobSubmitDateTime", "")),
    )
    for attr, key in (("error_reports", "ErrorReports"), ("task_count", "TaskCount")):
        try:
            setattr(jm, attr, int(rec.get(key, "0") or 0))
        except ValueError:
            pass
    # Group code: prefer the job-name prefix (always present — "GTE_…"), then
    # fall back to the PRISM_JOB path in the job environment.
    jm.group_code = group_from_job_name(jm.name)
    if not jm.group_code:
        m = _PRISM_JOB_RE.search(rec.get("EnvironmentDictionary", "") or "")
        if m:
            jm.group_code = m.group(1).upper()
    return jm


def is_excluded_jobmeta(jm: JobMeta) -> bool:
    """Drop Nuke comp jobs (by plugin) and Prism cleanup jobs (by name)."""
    if jm.plugin.strip().lower() == "nuke":
        return True
    return is_excluded_job(jm.name)


def query_jobs(dc: str, statuses: Iterable[str],
               since: timedelta | None = None) -> list[JobMeta]:
    """Query the Deadline DB for jobs in the given statuses, newest-first.

    One GetJobsFilter call per status (each ~1 s). `since` filters on the real
    JobSubmitDateTime client-side.
    """
    cutoff = (datetime.now() - since) if since else None
    seen: set[str] = set()
    jobs: list[JobMeta] = []
    for status in statuses:
        for rec in _parse_kv_records(_run_dc(dc, "GetJobsFilter", f"Status={status}")):
            jm = jobmeta_from_record(rec)
            if not jm.job_id or jm.job_id in seen:
                continue
            seen.add(jm.job_id)
            if cutoff and jm.submit_dt and jm.submit_dt < cutoff:
                continue
            jobs.append(jm)
    jobs.sort(key=lambda j: j.submit_dt or datetime.min, reverse=True)
    return jobs


def report_files_for_jobmeta(dc: str, job_id: str) -> list[Path]:
    """Return the exact `.bz2` log-report paths for a job (read-only query)."""
    out: list[Path] = []
    for line in _run_dc(dc, "GetJobLogReportFilenames", job_id):
        line = line.strip()
        if line.lower().endswith((".bz2", ".txt")):
            out.append(Path(line))
    return out


def _search1(rx: re.Pattern, text: str, group: int = 1, default: str = "") -> str:
    m = rx.search(text)
    return m.group(group) if m else default


def parse_textures(text: str, renderer: str) -> dict:
    """Extract texture inventory from the OpenImageIO ImageCache stats block.

    The block only appears in Karma logs (Karma uses OIIO) and only at the end
    of a successful frame. Renderman uses its own texture engine and does not
    print this block unless `ri:statistics:texturestatslevel` is enabled.

    Returns counts: total / native (.rat or .tex per renderer) / tiled-non-native /
    untiled, plus a per-extension breakdown, plus a `data_found` flag.
    """
    out = {"data_found": False, "total": 0, "native": 0, "tiled_nonnative": 0,
           "untiled": 0, "ext_counts": {}}
    m = OIIO_BLOCK_RE.search(text)
    if not m:
        return out
    out["data_found"] = True
    block = m.group(1)
    native_ext = NATIVE_TEXTURE_EXT.get(renderer)
    seen: set[str] = set()
    for fm in OIIO_FILE_LINE_RE.finditer(block):
        path = fm.group(1)
        if path in seen:
            continue
        seen.add(path)
        ext = fm.group(2).lower()
        untiled = bool(fm.group(3))
        out["total"] += 1
        out["ext_counts"][ext] = out["ext_counts"].get(ext, 0) + 1
        if native_ext and ext == native_ext:
            out["native"] += 1
        elif untiled:
            out["untiled"] += 1
        else:
            out["tiled_nonnative"] += 1
    return out


def parse_warnings(text: str) -> list[str]:
    """Collect render-engine warnings (Karma/Husk + Renderman codes)."""
    found: list[str] = []
    for m in WARNING_RE.finditer(text):
        found.append(m.group(1).strip())
    for m in RENDERMAN_CODE_RE.finditer(text):
        found.append(m.group(1).strip())
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for line in found:
        # Collapse heavy repetition: trim path-specific suffixes for dedup key
        key = re.sub(r"/[\w/.\-]+", "/…", line, count=1)
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


IMAGE_EXTS = {"exr", "tif", "tiff", "jpg", "jpeg", "png", "rat", "tex", "tx", "hdr"}
# Files that, by format, cannot be mipmapped. Anything else (.exr/.tif) might be
# tiled and serve as a mipmap; we can't tell from the path alone, so we treat
# them as "tiled_nonnative" — usable but off-pipeline-spec.
UNMIPPABLE_EXTS = {"jpg", "jpeg", "png", "hdr"}
UDIM_TOKEN_RE = re.compile(r"<[^>]+>")


def gather_usd_textures(usd_path: str, renderer: str) -> dict:
    """Walk the composed USD stage and inventory texture asset references.

    Returns the same shape as parse_textures(), plus on-disk bytes and the
    count of texture files we could not reach (drive not mapped, etc.).
    Status lives in the "source" field: "usd" on success, otherwise
    "no_pxr" / "open_failed" / "unreachable".
    """
    out = {"data_found": False, "source": "", "total": 0, "native": 0,
           "tiled_nonnative": 0, "untiled": 0, "ext_counts": {},
           "disk_bytes": 0, "unreachable": 0}
    try:
        from pxr import Usd, Sdf  # type: ignore
    except ImportError:
        out["source"] = "no_pxr"
        return out
    import os
    if not usd_path or not os.path.exists(usd_path):
        out["source"] = "unreachable"
        return out
    try:
        stage = Usd.Stage.Open(usd_path, Usd.Stage.LoadAll)
    except Exception:
        out["source"] = "open_failed"
        return out
    if not stage:
        out["source"] = "open_failed"
        return out

    native_ext = NATIVE_TEXTURE_EXT.get(renderer)
    seen: set[str] = set()
    for prim in stage.Traverse():
        for attr in prim.GetAttributes():
            try:
                val = attr.Get()
            except Exception:
                continue
            if not isinstance(val, Sdf.AssetPath):
                continue
            raw = val.resolvedPath or val.path
            if not raw or raw in seen:
                continue
            # Strip <UDIM>/<UV>/<f> tokens before extracting the extension.
            clean = UDIM_TOKEN_RE.sub("", raw)
            ext = clean.rsplit(".", 1)[-1].lower() if "." in clean else ""
            if ext not in IMAGE_EXTS:
                continue
            seen.add(raw)
            out["total"] += 1
            out["ext_counts"][ext] = out["ext_counts"].get(ext, 0) + 1
            try:
                out["disk_bytes"] += os.path.getsize(raw)
            except OSError:
                out["unreachable"] += 1
            if native_ext and ext == native_ext:
                out["native"] += 1
            elif ext in UNMIPPABLE_EXTS:
                out["untiled"] += 1
            else:
                out["tiled_nonnative"] += 1

    out["data_found"] = True
    out["source"] = "usd"
    return out


def gather_usd_deps(usd_path: str) -> tuple[int, int, str]:
    """Open a USD stage and return (dep_count, total_bytes, status).

    Requires `pxr.Usd` (install via `pip install usd-core`, or run with hython).
    Status: "ok" | "no_pxr" | "open_failed" | "unreachable".
    """
    try:
        from pxr import Usd  # type: ignore
    except ImportError:
        return 0, 0, "no_pxr"
    import os
    if not usd_path or not os.path.exists(usd_path):
        return 0, 0, "unreachable"
    try:
        stage = Usd.Stage.Open(usd_path, Usd.Stage.LoadAll)
    except Exception:
        return 0, 0, "open_failed"
    if not stage:
        return 0, 0, "open_failed"
    total_bytes = 0
    count = 0
    for layer in stage.GetUsedLayers(includeClipLayers=True):
        real = getattr(layer, "realPath", "") or ""
        if not real:
            continue
        try:
            total_bytes += os.path.getsize(real)
            count += 1
        except OSError:
            pass
    return count, total_bytes, "ok"


def parse_report(text: str, source: str) -> Report:
    r = Report(source_file=source)

    m = JOB_LOADED_RE.search(text)
    if m:
        r.job_name, r.job_id = m.group(1), m.group(2)
    # Successful logs skip the 'Loaded job:' line — fall back to the wrapper .py
    # filename, which encodes the job name + a timestamp suffix.
    if not r.job_name:
        m = WRAPPER_PY_RE.search(text)
        if m:
            r.job_name = m.group(1)

    r.job_user = _search1(JOB_USER_RE, text)
    r.machine_user = _search1(RUNNING_AS_RE, text)
    r.worker_name = _search1(WORKER_NAME_RE, text)
    r.worker_os = _search1(WORKER_OS_RE, text).strip()
    r.worker_gpu = _search1(WORKER_GPU_RE, text).strip()
    r.submit_date = _search1(SUBMIT_DATE_RE, text).strip()
    r.completion_date = _search1(COMPLETION_DATE_RE, text).strip()
    r.frame = _search1(FRAME_RE, text)

    m = WORKER_MEM_RE.search(text)
    if m:
        r.worker_total_gib = float(m.group(2))
        r.worker_tier_gib = _classify_slave_tier(r.worker_total_gib)

    m = WORKER_CPUS_RE.search(text)
    if m:
        r.worker_cpus = int(m.group(1))

    r.renderer = _search1(HUSK_RENDERER_RE, text)

    m = HUSK_OUTPUT_RE.search(text)
    if m:
        r.output_path = m.group(1)
        r.output_drive = m.group(1)[:2]

    # Pick the first .usd/.usdc that isn't the husk.exe — first match works
    # because husk.exe path contains spaces, which the regex excludes.
    m = HUSK_INPUT_USD_RE.search(text)
    if m:
        r.input_usd = m.group(1)

    # Group code: the job name always starts with "<GROUP>_" — the reliable
    # signal. Fall back to the L5_xxxx/<GROUP>/ folder in the USD path.
    r.group_code = group_from_job_name(r.job_name)
    if not r.group_code and r.input_usd:
        r.group_code = _group_from_path(r.input_usd)

    for m in DRIVE_MAP_RE.finditer(text):
        r.drive_map[m.group(1)] = m.group(2)

    # Elapsed time — prefer Details block (failures), fall back to start→end ts.
    # Deadline format is DAYS:HOURS:MINUTES:SECONDS (verified: 00:03:00:11 on a
    # 3h-timeout job means 3 hours, not 3 minutes).
    m = ELAPSED_RE.search(text)
    if m:
        d, h, mn, s = m.groups()
        r.elapsed_sec = int(d) * 86400 + int(h) * 3600 + int(mn) * 60 + int(s)
        r.elapsed_source = "details"

    # Peak RAM — prefer husk's self-report (more accurate than Deadline's sample).
    husk_peaks = [float(x) for x in HUSK_PEAK_MEM_RE.findall(text)]
    if husk_peaks:
        r.peak_ram_gib = max(husk_peaks)
        r.peak_ram_source = "husk"
    else:
        m = PEAK_RAM_DETAILS_RE.search(text)
        if m:
            r.peak_ram_gib = int(m.group(1)) / (1024 ** 3)
            r.peak_ram_source = "deadline"

    m = AVG_RAM_DETAILS_RE.search(text)
    if m:
        r.avg_ram_gib = int(m.group(1)) / (1024 ** 3)

    # AOVs (dedupe; some logs print multiple JSON dumps)
    r.aov_names = AOV_NAME_RE.findall(text)
    r.aov_count = len(set(r.aov_names))

    # Texture inventory + mipmap status (OIIO stats block, end of log)
    tex = parse_textures(text, r.renderer)
    r.texture_data_found = tex["data_found"]
    if tex["data_found"]:
        r.texture_source = "oiio"
    r.texture_total = tex["total"]
    r.texture_native = tex["native"]
    r.texture_tiled_nonnative = tex["tiled_nonnative"]
    r.texture_untiled = tex["untiled"]
    r.texture_ext_counts = tex["ext_counts"]

    # Render-engine warnings (Karma/Husk + Renderman R/S codes)
    r.warnings = parse_warnings(text)

    # Karma progress / memory curve
    for m in KARMA_PROGRESS_RE.finditer(text):
        pct = float(m.group(1))
        lap = _hms_to_sec(m.group(2))
        mem = float(m.group(4))
        r.karma_progress_curve.append((lap, pct))
        r.karma_mem_curve.append((lap, mem))

    # Outcome — first matching wins; order matters
    if TASK_TIMEOUT_RE.search(text):
        r.outcome = "timeout"
    elif USD_LOAD_FAIL_RE.search(text):
        r.outcome = "usd_load_failed"
    elif HUSK_PATH_ERROR_RE.search(text):
        r.outcome = "env_config_error"
    elif SUCCESS_RE.search(text):
        r.outcome = "success"
    else:
        exits = EXIT_CODE_RE.findall(text)
        r.outcome = "failed" if exits and exits[-1] != "0" else "unknown"

    # Fallback elapsed via timestamps when Details has no Elapsed Time
    if r.elapsed_sec == 0:
        start = end = None
        for line in text.splitlines():
            tm = LINE_TS_RE.match(line)
            if not tm:
                continue
            ts = datetime.strptime(tm.group(1), "%Y-%m-%d %H:%M:%S")
            if start is None and "Plugin rendering frame(s)" in line:
                start = ts
            if "task completed successfully" in line or "Process exit code" in line:
                end = ts
        if start and end:
            r.elapsed_sec = max(0, int((end - start).total_seconds()))
            r.elapsed_source = "timestamps"

    return r


# --- Analyzer -------------------------------------------------------------

def analyze(r: Report) -> Report:
    flags: list[Flag] = []

    if r.outcome == "timeout":
        flags.append(Flag("timeout", SEV_CRIT,
                          f"Task timed out after {fmt_hms(r.elapsed_sec)} — Deadline killed it."))
    elif r.outcome == "usd_load_failed":
        flags.append(Flag("usd_load_failed", SEV_HIGH,
                          f"Husk could not load the input USD ({r.input_usd or 'unknown path'})."))
    elif r.outcome == "env_config_error":
        flags.append(Flag("env_config_error", SEV_MED,
                          "Worker had no HUSK_PATH / PRISM_DEADLINE_HUSK_PATH set — render never started."))
    elif r.outcome == "failed":
        flags.append(Flag("non_zero_exit", SEV_HIGH, "Render process exited with non-zero code."))

    if r.elapsed_sec > FRAME_TIME_LIMIT_SEC:
        flags.append(Flag("long_frame", SEV_HIGH,
                          f"Frame took {fmt_hms(r.elapsed_sec)} (> 1h30 ceiling)."))

    if r.aov_count > AOV_LIMIT:
        flags.append(Flag("aov_excess", SEV_MED,
                          f"{r.aov_count} AOVs in render product (> {AOV_LIMIT} ceiling)."))

    if r.output_drive and r.output_drive.upper() == DATA_PFE_DRIVE:
        flags.append(Flag("output_to_data_pfe", SEV_CRIT,
                          f"Output drive is {r.output_drive} (DATA_PFE) — must be {DATA_FRM_DRIVE} (DATA_FRM)."))

    # Slave RAM saturation: peak within 1 GiB of slave's total
    if r.worker_tier_gib and r.peak_ram_gib:
        floor = r.worker_tier_gib - RAM_HEADROOM_GIB
        if r.peak_ram_gib >= floor:
            flags.append(Flag("ram_saturation", SEV_CRIT,
                              f"Peak RAM {r.peak_ram_gib:.1f} GiB on a {r.worker_tier_gib} GiB worker — "
                              f"≤ {RAM_HEADROOM_GIB:.0f} GiB headroom. Risks swap/crash."))
        else:
            # Won't-fit-smaller-slave informational flag
            if r.peak_ram_gib >= 31:
                flags.append(Flag("wont_fit_32gb", SEV_MED,
                                  f"Peak RAM {r.peak_ram_gib:.1f} GiB — cannot run on 32 GiB workers."))
            elif r.peak_ram_gib >= 15:
                flags.append(Flag("wont_fit_16gb", SEV_LOW,
                                  f"Peak RAM {r.peak_ram_gib:.1f} GiB — cannot run on 16 GiB workers."))

    # Karma memory grower
    if len(r.karma_mem_curve) >= 5:
        start_mem = min(mem for _, mem in r.karma_mem_curve[:3])
        end_mem = r.karma_mem_curve[-1][1]
        growth = end_mem - start_mem
        if growth >= MEMORY_GROWTH_FLAG_GIB:
            flags.append(Flag("memory_grower", SEV_HIGH,
                              f"Karma RAM climbed {growth:.1f} GiB during render "
                              f"({start_mem:.1f} → {end_mem:.1f} GiB)."))

    # Stuck progress
    if r.karma_progress_curve:
        last_lap, last_pct = r.karma_progress_curve[-1]
        if last_lap > STUCK_PROGRESS_AFTER_MIN * 60 and last_pct < STUCK_PROGRESS_PCT:
            flags.append(Flag("stuck_progress", SEV_CRIT,
                              f"Only {last_pct:.1f}% rendered after {fmt_hms(int(last_lap))} — non-converging."))

    # Mipmap / texture format. Native: .rat (Karma) or .tex (Renderman).
    # Tiled .exr/.tif counts as usable but off-spec; untiled = real problem.
    native_ext = NATIVE_TEXTURE_EXT.get(r.renderer, "")
    if r.texture_total > 0 and native_ext:
        non_native = r.texture_total - r.texture_native
        if non_native > 0:
            ratio = non_native / r.texture_total
            if r.texture_untiled > 0:
                sev = SEV_HIGH if ratio >= 0.5 else SEV_MED
                flags.append(Flag("mipmap_missing", sev,
                                  f"{r.texture_untiled}/{r.texture_total} textures are UNTILED "
                                  f"(no mipmaps). {r.texture_native} use the native .{native_ext} format."))
            else:
                flags.append(Flag("mipmap_offspec", SEV_LOW,
                                  f"{non_native}/{r.texture_total} textures use tiled .exr/.tif instead of "
                                  f"the pipeline-spec .{native_ext} — usable, but should be converted."))

    # Low-severity render warnings (informational diagnostics)
    if r.warnings:
        sample = r.warnings[:WARN_SAMPLE_COUNT]
        extra = f" (+{len(r.warnings) - len(sample)} more)" if len(r.warnings) > len(sample) else ""
        flags.append(Flag("render_warnings", SEV_LOW,
                          f"{len(r.warnings)} distinct warning(s) from the renderer{extra}: "
                          + " | ".join(sample)))

    # USD dependency stats (only flagged when count is unusually high)
    if r.usd_inspect_status == "ok" and r.usd_dep_count >= 200:
        flags.append(Flag("usd_dep_heavy", SEV_MED,
                          f"{r.usd_dep_count} USD layers ({r.usd_dep_bytes/1024**2:.1f} MiB) "
                          "composed for this render — heavy scene graph."))

    r.flags = flags
    r.severity = max((f.severity for f in flags), default=SEV_INFO, key=SEV_ORDER.get)
    return r


def fmt_hms(sec: int) -> str:
    sec = int(sec)
    if sec <= 0:
        return "0s"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def renderer_label(r: Report) -> str:
    if r.renderer == KARMA_RENDERER:
        return "Karma"
    if r.renderer == RENDERMAN_RENDERER:
        return "Renderman"
    return r.renderer or "?"


# --- Writers --------------------------------------------------------------

CSV_FIELDS = [
    "source_file", "severity", "outcome", "job_status", "plugin",
    "group_code", "job_user", "job_id",
    "job_name", "frame", "renderer", "elapsed_sec", "elapsed_hms",
    "peak_ram_gib", "peak_ram_source", "worker_tier_gib", "worker_name",
    "aov_count", "output_drive", "input_usd",
    "tex_source", "tex_total", "tex_native", "tex_tiled_nonnative", "tex_untiled",
    "tex_exts", "tex_disk_mib", "tex_unreachable",
    "warning_count", "usd_dep_count", "usd_dep_mib", "usd_inspect_status",
    "flag_codes", "flag_messages",
]


def to_csv_row(r: Report) -> dict:
    return {
        "source_file": r.source_file,
        "severity": r.severity,
        "outcome": r.outcome,
        "job_status": r.job_status,
        "plugin": r.plugin,
        "group_code": r.group_code,
        "job_user": r.job_user,
        "job_id": r.job_id,
        "job_name": r.job_name,
        "frame": r.frame,
        "renderer": renderer_label(r),
        "elapsed_sec": r.elapsed_sec,
        "elapsed_hms": fmt_hms(r.elapsed_sec),
        "peak_ram_gib": f"{r.peak_ram_gib:.2f}" if r.peak_ram_gib else "",
        "peak_ram_source": r.peak_ram_source,
        "worker_tier_gib": r.worker_tier_gib or "",
        "worker_name": r.worker_name,
        "aov_count": r.aov_count,
        "output_drive": r.output_drive,
        "input_usd": r.input_usd,
        "tex_source": r.texture_source,
        "tex_total": r.texture_total,
        "tex_native": r.texture_native,
        "tex_tiled_nonnative": r.texture_tiled_nonnative,
        "tex_untiled": r.texture_untiled,
        "tex_exts": ";".join(f"{ext}:{n}" for ext, n in sorted(r.texture_ext_counts.items())),
        "tex_disk_mib": f"{r.texture_disk_bytes/1024**2:.1f}" if r.texture_disk_bytes else "",
        "tex_unreachable": r.texture_unreachable,
        "warning_count": len(r.warnings),
        "usd_dep_count": r.usd_dep_count,
        "usd_dep_mib": f"{r.usd_dep_bytes/1024**2:.1f}" if r.usd_dep_bytes else "",
        "usd_inspect_status": r.usd_inspect_status,
        "flag_codes": ";".join(f.code for f in r.flags),
        "flag_messages": " | ".join(f.message for f in r.flags),
    }


def write_csv(reports: Iterable[Report], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in reports:
            w.writerow(to_csv_row(r))


SEV_BORDER = {
    SEV_INFO: "#475569",
    SEV_LOW: "#3b82f6",
    SEV_MED: "#f59e0b",
    SEV_HIGH: "#f97316",
    SEV_CRIT: "#dc2626",
}


def to_html_card(r: Report) -> str:
    color = SEV_BORDER.get(r.severity, "#475569")
    flag_html = "".join(
        f'<li><span class="flag-code">{html.escape(f.code)}</span>'
        f'<span class="flag-sev sev-{f.severity}">{f.severity}</span>'
        f'{html.escape(f.message)}</li>'
        for f in r.flags
    ) or '<li class="ok">No flags raised.</li>'

    if r.texture_data_found and r.texture_total:
        ext_summary = ", ".join(f"{ext}:{n}" for ext, n in sorted(r.texture_ext_counts.items()))
        extra = ""
        if r.texture_source == "usd":
            disk_mib = r.texture_disk_bytes / 1024**2
            unreachable = (f", {r.texture_unreachable} unreachable"
                           if r.texture_unreachable else "")
            extra = (f" <span class='muted'>· source: USD scene · "
                     f"{disk_mib:.1f} MiB on disk{unreachable}</span>")
        else:
            extra = " <span class='muted'>· source: OIIO runtime stats</span>"
        tex_html = (
            f"{r.texture_total} files — native: {r.texture_native}, "
            f"tiled non-native: {r.texture_tiled_nonnative}, untiled: {r.texture_untiled} "
            f"<span class='muted'>[{ext_summary}]</span>{extra}"
        )
    elif r.texture_data_found:
        tex_html = '0 files <span class="muted">(no textures used)</span>'
    elif r.texture_source in ("no_pxr", "open_failed", "unreachable"):
        tex_html = f'<span class="muted">no data — USD inspection failed: {html.escape(r.texture_source)}</span>'
    elif renderer_label(r) == "Renderman":
        tex_html = ('<span class="muted">no data — enable '
                    '<code>ri:statistics:texturestatslevel</code> or run with '
                    '<code>--inspect-usd</code></span>')
    else:
        tex_html = ('<span class="muted">no data — render did not reach OIIO stats '
                    '(failed/timeout before frame end)</span>')

    if r.usd_inspect_status == "ok":
        usd_dep_html = (f"{r.usd_dep_count} layers, "
                        f"{r.usd_dep_bytes/1024**2:.1f} MiB on disk")
    elif r.usd_inspect_status:
        usd_dep_html = f'<span class="muted">{html.escape(r.usd_inspect_status)}</span>'
    else:
        usd_dep_html = '<span class="muted">not inspected</span>'

    warn_html = ""
    if r.warnings:
        sample = r.warnings[:WARN_SAMPLE_COUNT]
        items = "".join(f"<li>{html.escape(w)}</li>" for w in sample)
        extra = (f"<li class='muted'>… {len(r.warnings) - len(sample)} more</li>"
                 if len(r.warnings) > len(sample) else "")
        warn_html = f'<details class="warnings"><summary>{len(r.warnings)} render warning(s)</summary><ul>{items}{extra}</ul></details>'

    return f"""
<div class="card" style="border-left-color:{color}">
  <header>
    <span class="sev sev-{r.severity}">{r.severity.upper()}</span>
    <span class="group">{html.escape(r.group_code or '—')}</span>
    <span class="user">{html.escape(r.job_user or '—')}</span>
    <span class="renderer">{html.escape(renderer_label(r))}</span>
    <span class="outcome outcome-{r.outcome}">task: {html.escape(r.outcome)}</span>
    {f'<span class="jobstatus">job: {html.escape(r.job_status)}</span>' if r.job_status else ''}
  </header>
  <h3>{html.escape(r.job_name or r.source_file)}</h3>
  <dl>
    <dt>Frame</dt><dd>{html.escape(r.frame or '—')}</dd>
    <dt>Elapsed</dt><dd>{fmt_hms(r.elapsed_sec)} <span class="muted">({r.elapsed_source or '?'})</span></dd>
    <dt>Peak RAM</dt><dd>{f"{r.peak_ram_gib:.1f} GiB" if r.peak_ram_gib else '—'} <span class="muted">from {r.peak_ram_source or '?'} · worker {r.worker_tier_gib or '?'} GiB ({html.escape(r.worker_name or '?')})</span></dd>
    <dt>AOVs</dt><dd>{r.aov_count}</dd>
    <dt>Textures</dt><dd>{tex_html}</dd>
    <dt>USD deps</dt><dd>{usd_dep_html}</dd>
    <dt>Output</dt><dd class="path">{html.escape(r.output_drive)} {html.escape(r.output_path or '')}</dd>
    <dt>USD in</dt><dd class="path">{html.escape(r.input_usd or '—')}</dd>
    <dt>Source</dt><dd class="path">{html.escape(r.source_file)}</dd>
  </dl>
  <ul class="flags">{flag_html}</ul>
  {warn_html}
</div>
""".strip()


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Deadline Cleaner — render risk report</title>
<style>
  body { font: 14px/1.4 -apple-system, "Segoe UI", system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 24px; }
  h1 { margin: 0 0 4px; }
  .summary { color: #94a3b8; margin-bottom: 16px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .card { background: #1e293b; border-left: 4px solid #475569; border-radius: 6px; padding: 12px 16px; margin-bottom: 12px; }
  .card header { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; font-size: 12px; }
  .card h3 { margin: 8px 0; font-size: 14px; font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; word-break: break-all; color: #f8fafc; }
  dl { display: grid; grid-template-columns: 90px 1fr; gap: 2px 12px; margin: 8px 0 6px; font-size: 12px; }
  dt { color: #94a3b8; }
  dd { margin: 0; }
  dd.path { font-family: ui-monospace, Consolas, monospace; font-size: 11px; word-break: break-all; color: #cbd5e1; }
  .muted { color: #64748b; font-size: 11px; }
  .sev, .flag-sev { padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .sev-critical { background: #7f1d1d; color: #fecaca; }
  .sev-high     { background: #9a3412; color: #fed7aa; }
  .sev-medium   { background: #78350f; color: #fde68a; }
  .sev-low      { background: #1e3a8a; color: #bfdbfe; }
  .sev-info     { background: #334155; color: #cbd5e1; }
  .group        { background: #0ea5e9; color: #082f49; padding: 2px 6px; border-radius: 3px; font-weight: 700; }
  .user, .renderer, .outcome { color: #94a3b8; }
  .jobstatus { color: #f87171; font-weight: 600; }
  .outcome-success { color: #4ade80; }
  .outcome-timeout, .outcome-failed, .outcome-usd_load_failed { color: #f87171; }
  ul.flags { margin: 4px 0 0; padding-left: 18px; font-size: 12px; }
  ul.flags li { margin: 3px 0; }
  ul.flags .flag-code { font-family: ui-monospace, Consolas, monospace; color: #fbbf24; margin-right: 6px; }
  ul.flags .flag-sev { margin-right: 6px; }
  ul.flags .ok { color: #4ade80; list-style: none; margin-left: -18px; }
  details.warnings { margin-top: 6px; font-size: 11px; }
  details.warnings summary { color: #fbbf24; cursor: pointer; }
  details.warnings ul { margin: 6px 0 0; padding-left: 18px; color: #cbd5e1; }
  details.warnings li { margin: 1px 0; font-family: ui-monospace, Consolas, monospace; }
  .hdr-total { font-weight: 600; color: #f8fafc; margin-right: 8px; }
  .group-nav { margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 6px; }
  .group-chip { background: #1e293b; border: 1px solid #334155; border-radius: 4px;
                padding: 4px 10px; color: #e2e8f0; text-decoration: none; font-size: 12px; }
  .group-chip:hover { background: #334155; border-color: #475569; }
  details.group-block { background: #0b1220; border: 1px solid #1e293b; border-radius: 8px;
                        padding: 10px 14px; margin: 12px 0; }
  details.group-block > summary { cursor: pointer; font-size: 16px; padding: 4px 0;
                                  list-style: none; display: flex; align-items: center; gap: 10px; }
  details.group-block > summary::-webkit-details-marker { display: none; }
  details.group-block > summary::before { content: "▶"; font-size: 10px; color: #64748b;
                                          transition: transform .15s; }
  details.group-block[open] > summary::before { transform: rotate(90deg); }
  details.user-block { margin: 8px 0 8px 18px; }
  details.user-block > summary { cursor: pointer; font-size: 13px; padding: 4px 0;
                                 list-style: none; display: flex; align-items: center; gap: 8px;
                                 color: #cbd5e1; }
  details.user-block > summary::-webkit-details-marker { display: none; }
  details.user-block > summary::before { content: "▶"; font-size: 9px; color: #64748b;
                                         transition: transform .15s; }
  details.user-block[open] > summary::before { transform: rotate(90deg); }
  details.user-block .user-name { font-weight: 600; color: #f1f5f9; }
  .user-cards { margin: 4px 0 0 16px; }
</style>
</head>
<body>
<h1>Deadline Cleaner — render risk report</h1>
__SUMMARY____CARDS__
</body>
</html>
"""


def _max_sev(reports: Iterable[Report]) -> str:
    return max((r.severity for r in reports), default=SEV_INFO, key=SEV_ORDER.get)


def _group_reports(reports: list[Report]) -> dict[str, dict[str, list[Report]]]:
    """Bucket reports into {group: {user: [reports]}} preserving sort order."""
    out: dict[str, dict[str, list[Report]]] = {}
    for r in reports:
        g = bucket_group(r.group_code)
        u = r.job_user or "(unknown)"
        out.setdefault(g, {}).setdefault(u, []).append(r)
    return out


def write_html(reports: list[Report], path: Path) -> None:
    counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    for r in reports:
        counts[r.severity] = counts.get(r.severity, 0) + 1
        group_counts[bucket_group(r.group_code)] = group_counts.get(bucket_group(r.group_code), 0) + 1

    summary_parts = [f"<span class='hdr-total'>{len(reports)} job(s) analyzed</span>"]
    for sev in [SEV_CRIT, SEV_HIGH, SEV_MED, SEV_LOW, SEV_INFO]:
        if counts.get(sev):
            summary_parts.append(f"<span class='sev sev-{sev}'>{counts[sev]} {sev}</span>")
    group_chips = "".join(
        f"<a class='group-chip' href='#g-{g}'>{g} <span class='muted'>×{group_counts[g]}</span></a>"
        for g in GROUP_BUCKETS if group_counts.get(g)
    )

    grouped = _group_reports(reports)
    sections: list[str] = []
    for g in GROUP_BUCKETS:
        if g not in grouped:
            continue
        users = grouped[g]
        group_reports = [r for rs in users.values() for r in rs]
        worst = _max_sev(group_reports)
        # Auto-expand groups that have something to triage
        open_attr = " open" if worst in (SEV_CRIT, SEV_HIGH) else ""
        user_blocks: list[str] = []
        # Sort users by worst-severity descending, then by name
        for user in sorted(users, key=lambda u: (-SEV_ORDER[_max_sev(users[u])], u.lower())):
            urs = users[user]
            u_worst = _max_sev(urs)
            cards = "\n".join(to_html_card(r) for r in urs)
            user_open = " open" if u_worst in (SEV_CRIT, SEV_HIGH) else ""
            user_blocks.append(
                f"<details class='user-block'{user_open}>"
                f"<summary><span class='sev sev-{u_worst}'>{u_worst}</span> "
                f"<span class='user-name'>{html.escape(user)}</span> "
                f"<span class='muted'>· {len(urs)} job(s)</span></summary>"
                f"<div class='user-cards'>{cards}</div></details>"
            )
        sections.append(
            f"<section id='g-{g}'><details class='group-block'{open_attr}>"
            f"<summary><span class='group'>{g}</span> "
            f"<span class='sev sev-{worst}'>worst: {worst}</span> "
            f"<span class='muted'>· {len(group_reports)} job(s) · {len(users)} user(s)</span></summary>"
            + "\n".join(user_blocks)
            + "</details></section>"
        )

    body = (
        f"<div class='summary'>{' '.join(summary_parts)}</div>"
        f"<div class='group-nav'>{group_chips}</div>"
        + "\n".join(sections)
    )
    out = HTML_TEMPLATE.replace("__SUMMARY__", "").replace("__CARDS__", body)
    path.write_text(out, encoding="utf-8")


def write_md_per_student(reports: list[Report], path: Path) -> None:
    grouped = _group_reports(reports)

    counts: dict[str, int] = {}
    for r in reports:
        counts[r.severity] = counts.get(r.severity, 0) + 1
    summary = ", ".join(f"{counts[s]} {s}" for s in [SEV_CRIT, SEV_HIGH, SEV_MED, SEV_LOW, SEV_INFO] if counts.get(s))

    lines = [
        "# Deadline Cleaner — render risk report",
        "",
        f"**{len(reports)} job(s)** — {summary}",
        "",
    ]
    for g in GROUP_BUCKETS:
        if g not in grouped:
            continue
        users = grouped[g]
        g_reports = [r for rs in users.values() for r in rs]
        g_worst = _max_sev(g_reports)
        lines.append(f"## {g} — worst: **{g_worst}** ({len(g_reports)} job(s), {len(users)} user(s))")
        lines.append("")
        for user in sorted(users, key=lambda u: (-SEV_ORDER[_max_sev(users[u])], u.lower())):
            rs = users[user]
            u_worst = _max_sev(rs)
            lines.append(f"### {user} — worst: **{u_worst}** ({len(rs)} job(s))")
            lines.append("")
            for r in sorted(rs, key=lambda r: -SEV_ORDER[r.severity]):
                ram = f"{r.peak_ram_gib:.1f} GiB" if r.peak_ram_gib else "—"
                if r.texture_data_found and r.texture_total:
                    tex = (f"{r.texture_total} tex ({r.texture_untiled} untiled, "
                           f"{r.texture_native} native)")
                elif r.texture_data_found:
                    tex = "0 tex"
                else:
                    tex = "no tex data"
                usd_dep = (f", {r.usd_dep_count} USD deps / {r.usd_dep_bytes/1024**2:.0f} MiB"
                           if r.usd_inspect_status == "ok" else "")
                lines.append(
                    f"- **[{r.severity}]** `{r.job_name or r.source_file}` — "
                    f"frame {r.frame or '?'}, {fmt_hms(r.elapsed_sec)}, peak RAM {ram} / "
                    f"{r.worker_tier_gib or '?'} GiB worker, AOVs {r.aov_count}, {tex}{usd_dep}, "
                    f"outcome `{r.outcome}`"
                )
                for f in r.flags:
                    lines.append(f"  - `{f.code}` ({f.severity}): {f.message}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --- CLI ------------------------------------------------------------------

VALID_OUTCOMES = ("success", "failed", "timeout", "usd_load_failed", "env_config_error")
SINCE_PRESETS = ("24h", "7d", "30d", "all")


def launch_gui(defaults: argparse.Namespace | None = None) -> int:
    """Open the Tkinter front-end. Returns 0 on normal close, 1 on error."""
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog
    except ImportError as e:
        print(f"error: Tkinter not available: {e}", file=sys.stderr)
        return 1
    import threading
    import webbrowser

    root = tk.Tk()
    root.title("Deadline Cleaner")
    root.geometry("680x680")
    root.minsize(560, 560)

    # ---- Deadline connection status (job discovery is via deadlinecommand)
    dc_path = find_deadlinecommand()
    dc_ok = dc_path is not None
    dc_msg = (f"Deadline Client found — querying the farm directly."
              if dc_ok else
              "deadlinecommand.exe NOT found — install Deadline Client or set DEADLINE_PATH.")
    ttk.Label(root, text=dc_msg,
              foreground=("#15803d" if dc_ok else "#b91c1c")).pack(anchor="w", padx=12, pady=(12, 2))

    # ---- Lookback (used only when Job IDs is empty)
    since_var = tk.StringVar(value=(defaults.since if defaults and defaults.since else "7d"))
    ttk.Label(root, text="Look back  (ignored if Job IDs are filled below):").pack(anchor="w", padx=12, pady=(10, 2))
    ttk.Combobox(root, textvariable=since_var, values=SINCE_PRESETS, state="readonly", width=10).pack(anchor="w", padx=12)

    # ---- Max-jobs slider. Caps the newest N jobs returned by deadlinecommand
    # (every frame of a kept job is read). Snaps to discrete stops; last = "no cap".
    LIMIT_STOPS = (5, 10, 20, 50, 100, 250, 500, 1000, 2500, 5000)
    ttk.Label(root, text="Max jobs to scan  (newest first):").pack(anchor="w", padx=12, pady=(10, 2))
    limit_row = ttk.Frame(root)
    limit_row.pack(fill="x", padx=12)
    limit_lbl = ttk.Label(limit_row, text="", width=22)
    limit_scale = ttk.Scale(limit_row, from_=0, to=len(LIMIT_STOPS), orient="horizontal")

    def _snap_limit(_v: str = "") -> int | None:
        """Snap to a discrete stop. Returns the job cap, or None for 'All'."""
        idx = max(0, min(len(LIMIT_STOPS), int(round(limit_scale.get()))))
        if idx == len(LIMIT_STOPS):
            limit_lbl.config(text="All jobs")
            return None
        n = LIMIT_STOPS[idx]
        limit_lbl.config(text=f"{n} jobs")
        return n

    limit_scale.configure(command=_snap_limit)
    limit_scale.set(4)  # default → 100 jobs
    limit_scale.pack(side="left", fill="x", expand=True)
    limit_lbl.pack(side="left", padx=(8, 0))
    _snap_limit()

    # ---- Manual job-ID selection
    ttk.Label(
        root,
        text="Job IDs  (optional — paste 24-hex IDs from Deadline Monitor, one per line):",
    ).pack(anchor="w", padx=12, pady=(10, 2))
    job_ids_text = tk.Text(root, height=4, font=("Consolas", 9),
                          bg="#0f172a", fg="#e2e8f0", insertbackground="#e2e8f0",
                          wrap="none")
    job_ids_text.pack(fill="x", padx=12)

    # ---- Groups
    ttk.Label(root, text="Groups (uncheck to exclude):").pack(anchor="w", padx=12, pady=(10, 2))
    groups_row = ttk.Frame(root)
    groups_row.pack(anchor="w", padx=12)
    group_vars: dict[str, tk.BooleanVar] = {}
    for i, g in enumerate(GROUP_BUCKETS):
        v = tk.BooleanVar(value=True)
        group_vars[g] = v
        ttk.Checkbutton(groups_row, text=g, variable=v).grid(row=0, column=i, padx=4, sticky="w")

    # ---- Job status (drives the deadlinecommand query)
    ttk.Label(root, text="Deadline job status to query:").pack(anchor="w", padx=12, pady=(10, 2))
    status_row = ttk.Frame(root)
    status_row.pack(anchor="w", padx=12)
    status_vars: dict[str, tk.BooleanVar] = {}
    for i, s in enumerate(JOB_STATUSES):
        v = tk.BooleanVar(value=(s in DEFAULT_JOB_STATUSES))
        status_vars[s] = v
        ttk.Checkbutton(status_row, text=s, variable=v).grid(row=i // 3, column=i % 3, padx=4, sticky="w")

    # ---- USD inspection
    usd_var = tk.BooleanVar(value=bool(defaults.inspect_usd) if defaults else False)
    ttk.Checkbutton(
        root,
        text="Inspect USD scenes (counts deps + classifies textures; needs pxr / hython; slower)",
        variable=usd_var,
    ).pack(anchor="w", padx=12, pady=(10, 2))

    # ---- Output dir
    out_var = tk.StringVar(value=(defaults.output_dir if defaults and defaults.output_dir else "report"))
    ttk.Label(root, text="Output folder:").pack(anchor="w", padx=12, pady=(10, 2))
    out_row = ttk.Frame(root)
    out_row.pack(fill="x", padx=12)
    ttk.Entry(out_row, textvariable=out_var).pack(side="left", fill="x", expand=True)
    ttk.Button(out_row, text="…", width=3,
               command=lambda: out_var.set(filedialog.askdirectory(initialdir=out_var.get() or ".") or out_var.get())
               ).pack(side="left", padx=(4, 0))

    # ---- Log area + progress
    log_frame = ttk.LabelFrame(root, text="Status")
    log_frame.pack(fill="both", expand=True, padx=12, pady=(12, 4))
    log_text = tk.Text(log_frame, height=10, state="disabled",
                      font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0", insertbackground="#e2e8f0")
    log_text.pack(fill="both", expand=True, padx=4, pady=4)
    progress = ttk.Progressbar(root, mode="determinate")
    progress.pack(fill="x", padx=12, pady=(0, 4))

    def log(msg: str) -> None:
        log_text.config(state="normal")
        log_text.insert("end", msg + "\n")
        log_text.see("end")
        log_text.config(state="disabled")

    def set_progress(done: int, total: int, msg: str) -> None:
        progress["maximum"] = max(total, 1)
        progress["value"] = done
        log(f"[{done}/{total}] {msg}")

    run_btn: ttk.Button  # forward decl

    def worker() -> None:
        try:
            if not dc_ok:
                log("error: deadlinecommand.exe not found — cannot query the farm.")
                return
            groups = {g for g, v in group_vars.items() if v.get()}
            job_statuses = [s for s, v in status_vars.items() if v.get()]
            out_dir = Path(out_var.get().strip() or "report")
            inspect_usd = usd_var.get()

            if not groups:
                log("error: no groups selected.")
                return

            # Manual job-ID selection wins over the status query
            raw_ids = job_ids_text.get("1.0", "end").strip()
            job_ids = [j for j in re.split(r"[,\s]+", raw_ids) if j.strip()]

            file_meta: dict[Path, JobMeta] = {}
            prog = lambda i, n, m: root.after(0, set_progress, i, n, m)

            if job_ids:
                log(f"Fetching {len(job_ids)} job ID(s) from Deadline…")
                files = []
                for jid in job_ids:
                    recs = _parse_kv_records(_run_dc(dc_path, "GetJob", jid))
                    jm = jobmeta_from_record(recs[0]) if recs else JobMeta(job_id=jid)
                    for f in report_files_for_jobmeta(dc_path, jid):
                        files.append(f)
                        file_meta[f] = jm
                log(f"  collected {len(files)} report file(s).")
            else:
                if not job_statuses:
                    log("error: no job status selected.")
                    return
                since = parse_since(since_var.get())
                cap = _snap_limit() or 0
                log(f"Querying Deadline  (status={','.join(job_statuses)}, "
                    f"since={since_var.get()}, max {cap or 'all'} jobs)…")
                files, file_meta, omitted = discover_deadline_reports(
                    dc_path, job_statuses, since,
                    max_jobs=cap, group_filter=groups, progress=prog,
                )
                if omitted:
                    log(f"  omitted {omitted} cleanup/nuke job(s).")
                log(f"  {len(files)} report file(s) to analyze.")

            if not files:
                log("Nothing to analyze. Widen the lookback, pick more statuses, "
                    "or paste valid Job IDs.")
                return

            reports = run_analysis(
                files, out_dir,
                inspect_usd=inspect_usd,
                groups=groups,
                job_meta_by_file=file_meta,
                progress=prog,
            )
            log(f"Kept {len(reports)} report(s) after filtering.")
            log(f"Wrote {out_dir/'report.html'}")
            log("Opening report…")
            webbrowser.open((out_dir / "report.html").resolve().as_uri())
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
        finally:
            root.after(0, lambda: run_btn.config(state="normal"))

    def on_run() -> None:
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")
        progress["value"] = 0
        run_btn.config(state="disabled")
        threading.Thread(target=worker, daemon=True).start()

    run_btn = ttk.Button(root, text="Analyze", command=on_run)
    run_btn.pack(pady=(4, 12), ipadx=20)

    root.mainloop()
    return 0


def is_excluded_job(name: str) -> bool:
    """Return True for housekeeping job names we don't want in the report."""
    if not name:
        return False
    low = name.lower()
    return any(frag in low for frag in EXCLUDED_JOB_NAME_FRAGMENTS)


def _relative_name_for(repo: Path):
    """Source-label factory: prefer 'jobid/<reportid>.txt' over absolute paths."""
    def _name(p: Path) -> str:
        try:
            return str(p.relative_to(repo))
        except ValueError:
            return p.name
    return _name


def analyze_file(fp: Path, source: str, *, inspect_usd: bool = False) -> Report:
    """Parse a single Deadline report file and run the analyzer on it.

    Transparently handles `.bz2`-compressed reports from the Repository share
    and plain `.txt` exports.
    """
    text = read_report_text(fp)
    r = parse_report(text, source=source)
    if inspect_usd:
        r.usd_dep_count, r.usd_dep_bytes, r.usd_inspect_status = gather_usd_deps(r.input_usd)
        if not r.texture_data_found:
            ut = gather_usd_textures(r.input_usd, r.renderer)
            if ut["data_found"]:
                r.texture_data_found = True
                r.texture_source = ut["source"]
                r.texture_total = ut["total"]
                r.texture_native = ut["native"]
                r.texture_tiled_nonnative = ut["tiled_nonnative"]
                r.texture_untiled = ut["untiled"]
                r.texture_ext_counts = ut["ext_counts"]
                r.texture_disk_bytes = ut["disk_bytes"]
                r.texture_unreachable = ut["unreachable"]
            else:
                r.texture_source = ut["source"]
    else:
        r.usd_inspect_status = "skipped"
    analyze(r)
    return r


def run_analysis(
    files: Iterable[Path],
    out_dir: Path,
    *,
    inspect_usd: bool = False,
    groups: set[str] | None = None,
    statuses: set[str] | None = None,
    name_for: "callable[[Path], str] | None" = None,
    progress: "callable[[int, int, str], None] | None" = None,
    workers: int = DEFAULT_WORKERS,
    job_meta_by_file: "dict[Path, JobMeta] | None" = None,
) -> list[Report]:
    """Parse + filter + write outputs. Returns the kept reports.

    SMB reads + bz2 decompression + parse run in parallel (`workers` threads).
    USD inspection — when enabled — runs in a second sequential pass on the
    kept reports, since pxr's stage API isn't safe to hammer from many threads.

    When `job_meta_by_file` is given, each report's job-level fields (name,
    user, group, status) are overridden with the authoritative deadlinecommand
    values instead of the less-reliable values scraped from the log text.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    files = list(files)
    total = len(files)
    name_for = name_for or (lambda p: p.name)
    meta_map = job_meta_by_file or {}

    def _parse_one(fp: Path) -> Report | None:
        try:
            text = read_report_text(fp)
            r = parse_report(text, source=name_for(fp))
        except Exception:
            return None
        jm = meta_map.get(fp)
        if jm:
            if jm.name:
                r.job_name = jm.name
            if jm.user:
                r.job_user = jm.user
            if jm.group_code:
                r.group_code = jm.group_code
            if jm.job_id:
                r.job_id = jm.job_id
            r.job_status = jm.status
            r.plugin = jm.plugin
        return r

    # Phase 1 — parallel read + parse (no analyzer yet, USD-free)
    parsed: list[Report] = []
    excluded = 0
    done = 0
    if progress:
        progress(0, total, f"reading {total} report(s) with {workers} workers…")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(_parse_one, fp) for fp in files):
            done += 1
            r = fut.result()
            if r is None:
                continue
            if is_excluded_job(r.job_name):
                excluded += 1
                continue
            if groups and bucket_group(r.group_code) not in groups:
                continue
            if statuses and r.outcome not in statuses:
                continue
            parsed.append(r)
            if progress and (done % 100 == 0 or done == total):
                progress(done, total, f"parsed {done}/{total} · kept {len(parsed)}")

    if progress and excluded:
        progress(total, total, f"omitted {excluded} cleanup/nuke report(s)")

    # Phase 2 — sequential USD inspection + analyze + flag
    for i, r in enumerate(parsed):
        if inspect_usd:
            r.usd_dep_count, r.usd_dep_bytes, r.usd_inspect_status = gather_usd_deps(r.input_usd)
            if not r.texture_data_found:
                ut = gather_usd_textures(r.input_usd, r.renderer)
                if ut["data_found"]:
                    r.texture_data_found = True
                    r.texture_source = ut["source"]
                    r.texture_total = ut["total"]
                    r.texture_native = ut["native"]
                    r.texture_tiled_nonnative = ut["tiled_nonnative"]
                    r.texture_untiled = ut["untiled"]
                    r.texture_ext_counts = ut["ext_counts"]
                    r.texture_disk_bytes = ut["disk_bytes"]
                    r.texture_unreachable = ut["unreachable"]
                else:
                    r.texture_source = ut["source"]
            if progress and (i % 5 == 0 or i == len(parsed) - 1):
                progress(i + 1, len(parsed), f"USD inspect {i+1}/{len(parsed)}")
        else:
            r.usd_inspect_status = "skipped"
        analyze(r)

    parsed.sort(key=lambda r: (
        bucket_group(r.group_code),
        r.job_user.lower(),
        -SEV_ORDER[r.severity],
        r.job_name,
    ))

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(parsed, out_dir / "report.csv")
    write_html(parsed, out_dir / "report.html")
    write_md_per_student(parsed, out_dir / "per_student.md")
    if progress:
        progress(total, total, f"done — kept {len(parsed)} report(s)")
    return parsed


def discover_deadline_reports(
    dc: str,
    statuses: Iterable[str],
    since: timedelta | None,
    *,
    max_jobs: int = 0,
    group_filter: set[str] | None = None,
    progress: "callable[[int, int, str], None] | None" = None,
) -> tuple[list[Path], dict[Path, JobMeta], int]:
    """Query the Deadline DB, then resolve every job's report files.

    Returns (report_files, file→JobMeta map, omitted_count). Read-only
    throughout — only GetJobsFilter / GetJobLogReportFilenames queries.
    """
    from concurrent.futures import ThreadPoolExecutor

    if progress:
        progress(0, 1, f"querying Deadline for {','.join(statuses)} jobs…")
    jobs = query_jobs(dc, statuses, since)

    kept: list[JobMeta] = []
    omitted = 0
    for jm in jobs:
        if is_excluded_jobmeta(jm):
            omitted += 1
            continue
        if group_filter and bucket_group(jm.group_code) not in group_filter:
            continue
        kept.append(jm)
    if max_jobs and max_jobs > 0:
        kept = kept[:max_jobs]

    if progress:
        progress(0, len(kept), f"{len(kept)} job(s) matched — resolving report files…")

    files: list[Path] = []
    file_meta: dict[Path, JobMeta] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
        for jm, rfiles in ex.map(lambda j: (j, report_files_for_jobmeta(dc, j.job_id)), kept):
            for f in rfiles:
                files.append(f)
                file_meta[f] = jm
            done += 1
            if progress and (done % 25 == 0 or done == len(kept)):
                progress(done, len(kept), f"resolved {done}/{len(kept)} jobs · {len(files)} reports")
    return files, file_meta, omitted


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input_dir", nargs="?", default=None,
                   help="Folder of Deadline report .txt files (legacy / manual export mode)")
    p.add_argument("--deadline", action="store_true",
                   help="Discover jobs via deadlinecommand (authoritative job list + "
                        "status). Recommended over --repo.")
    p.add_argument("--job-status", default=",".join(DEFAULT_JOB_STATUSES),
                   help="Comma-separated Deadline job statuses to query in --deadline "
                        f"mode (default: {','.join(DEFAULT_JOB_STATUSES)}). "
                        f"Valid: {','.join(JOB_STATUSES)}")
    p.add_argument("--repo", default=None,
                   help=f"Deadline Repository reports folder (fallback; e.g. {DEFAULT_REPO_PATH})")
    p.add_argument("--since", default="7d",
                   help="Lookback for --deadline / --repo scans: 24h / 7d / 30d / all (default: 7d)")
    p.add_argument("--job-ids", default="",
                   help="Comma-separated Deadline job IDs (24-hex). When set, "
                        "skips the time-based scan and reads only those job folders.")
    p.add_argument("--max-jobs", type=int, default=0,
                   help="Cap the scan to the newest N job folders (0 = no cap). "
                        "Capping on jobs (not report files) keeps an even spread "
                        "of distinct jobs. Only applies to --repo scans.")
    p.add_argument("--groups", default=",".join(GROUP_BUCKETS),
                   help="Comma-separated group codes to include (default: all). "
                        f"Known: {','.join(KNOWN_GROUPS)}, OTHER")
    p.add_argument("--status", default="",
                   help="Comma-separated outcomes to include (e.g. failed,timeout). "
                        "Default: all.")
    p.add_argument("-o", "--output-dir", default="report",
                   help="Where to write report files (default: ./report)")
    p.add_argument("--pattern", default="*.txt",
                   help="Glob for log files in legacy --input_dir mode (default: *.txt)")
    p.add_argument("--inspect-usd", action="store_true",
                   help="Open each input USD via pxr to count deps + classify textures. "
                        "Requires pxr.Usd (pip install usd-core, or run with hython).")
    p.add_argument("--gui", action="store_true",
                   help="Launch the Tkinter GUI (also the default when run with no args).")
    args = p.parse_args(argv)

    # GUI dispatch — no source args at all, or --gui flag
    if args.gui or (args.input_dir is None and args.repo is None and not args.deadline):
        return launch_gui(defaults=args)

    out_dir = Path(args.output_dir)
    groups = {g.strip().upper() for g in args.groups.split(",") if g.strip()}
    statuses = {s.strip() for s in args.status.split(",") if s.strip()} or None
    _cli_progress = lambda i, n, msg: print(f"[{i}/{n}] {msg}")

    # Preferred source: deadlinecommand (authoritative job list)
    if args.deadline:
        dc = find_deadlinecommand()
        if not dc:
            print("error: deadlinecommand.exe not found. Install Deadline Client "
                  "or set DEADLINE_PATH.", file=sys.stderr)
            return 2
        try:
            since = parse_since(args.since)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        job_id_list = [j for j in re.split(r"[,\s]+", args.job_ids) if j.strip()]
        file_meta: dict[Path, JobMeta] = {}
        if job_id_list:
            files = []
            for jid in job_id_list:
                recs = _parse_kv_records(_run_dc(dc, "GetJob", jid))
                jm = jobmeta_from_record(recs[0]) if recs else JobMeta(job_id=jid)
                rfiles = report_files_for_jobmeta(dc, jid)
                for f in rfiles:
                    files.append(f)
                    file_meta[f] = jm
            print(f"Found {len(files)} report(s) across {len(job_id_list)} job(s).")
        else:
            job_statuses = [s.strip() for s in args.job_status.split(",") if s.strip()]
            files, file_meta, omitted = discover_deadline_reports(
                dc, job_statuses, since,
                max_jobs=args.max_jobs, group_filter=groups,
                progress=_cli_progress,
            )
            if omitted:
                print(f"omitted {omitted} cleanup/nuke job(s)")
        if not files:
            print("warning: no matching jobs / reports found.", file=sys.stderr)
            return 0
        reports = run_analysis(
            files, out_dir,
            inspect_usd=args.inspect_usd,
            groups=groups, statuses=statuses,
            job_meta_by_file=file_meta,
            progress=_cli_progress,
        )
    # Fallback source: --repo file-share scan
    elif args.repo:
        repo = Path(args.repo)
        job_id_list = [j for j in re.split(r"[,\s]+", args.job_ids) if j.strip()]
        try:
            if job_id_list:
                files, missing = files_for_job_ids(repo, job_id_list)
                if missing:
                    print(f"warning: {len(missing)} job ID(s) not found: "
                          + ", ".join(missing[:5])
                          + (f" (+{len(missing)-5} more)" if len(missing) > 5 else ""),
                          file=sys.stderr)
                if not files:
                    print("error: none of the supplied job IDs had reports.", file=sys.stderr)
                    return 2
                print(f"Found {len(files)} report(s) across {len(job_id_list) - len(missing)} job(s).")
            else:
                since = parse_since(args.since)
                files = scan_repo(repo, since, limit=args.max_jobs or None)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

        reports = run_analysis(
            files, out_dir,
            inspect_usd=args.inspect_usd,
            groups=groups, statuses=statuses,
            name_for=_relative_name_for(repo),
            progress=lambda i, n, msg: print(f"[{i}/{n}] {msg}"),
        )
    else:
        in_dir = Path(args.input_dir)
        if not in_dir.is_dir():
            print(f"error: input dir not found: {in_dir}", file=sys.stderr)
            return 2
        files = sorted(in_dir.glob(args.pattern))
        if not files:
            print(f"warning: no files matched {args.pattern} in {in_dir}", file=sys.stderr)
            return 0
        reports = run_analysis(
            files, out_dir,
            inspect_usd=args.inspect_usd,
            groups=groups, statuses=statuses,
        )

    counts: dict[str, int] = {}
    for r in reports:
        counts[r.severity] = counts.get(r.severity, 0) + 1
    print(f"Analyzed {len(reports)} report(s):")
    for sev in [SEV_CRIT, SEV_HIGH, SEV_MED, SEV_LOW, SEV_INFO]:
        if sev in counts:
            print(f"  {sev:>8}: {counts[sev]}")
    print(f"Wrote {out_dir/'report.html'}")
    print(f"Wrote {out_dir/'report.csv'}")
    print(f"Wrote {out_dir/'per_student.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
