
> [!abstract] Summary
> **Deadline Log Analyser** — a Python tool by Thomas Spony that inspects Deadline render jobs and flags risky ones (timeouts, RAM issues, oversized USD scenes, un-mipmapped textures, wrong server output). Read-only — diagnoses, never modifies. Use cases: **daily triage** and **single-job check**. Output: HTML / CSV / Markdown reports grouped by Group → User → Job with severity badges.


---

# Build the .exe (one-time)

1. First replace in the py file where live your deadline repo : line DEFAULT_REPO_PATH = r"YOUR/REPO" line 60 

2. Change drive name and letters :line 47
DATA_PFE_DRIVE = "V:" 
DATA_FRM_DRIVE = "W:" 

3. 
Change organisation of log, for me I put the movie name but it will not fit your pipeline : 
KNOWN_GROUPS = ("HYT", "JRN", "VRT", "CNB", "PMF", "COR", "GTE")

4. 
```bash
# Install build tools (pip, not npm)
pip install usd-core pyinstaller

# Build the .exe
pyinstaller --onefile --windowed --name DeadlineCleaner --collect-all pxr deadline_cleaner.py
```

# What it flags

- [x] MIPMAP flagging.
- [x] AOV excess.
- [x] Memory pressure.
- [ ] USD dependencies.
- [ ] Non-indexed attribute `shop_materialpath` string.
- [ ] Invalid mesh.
- [ ] Bad primvar sample size.
- [ ] Displacement shader doesn't modify P.
- [ ] Singular matrix detected.
- [ ] *(Find online answers for the unchecked items.)*
- [ ] Need to integrate it with API directly



# What it's for

Student renders sometimes bring the farm — and the `DATA_PFE` server — to its knees. Deadline Cleaner reads logs Deadline already produced and flags the jobs most likely responsible so you can fix them before they crash the farm again.

Two typical uses:
- **Daily triage** — *"show me everything that failed on the farm this week."*
- **Single-job check** — *"why did *my* job fail / why was it so slow?"*

# Severity

| Badge | Meaning |
|---|---|
| 🔴 **critical** | Almost certainly hurting the farm — fix first. |
| 🟠 **high** | A real problem; fix soon. |
| 🟡 **medium** | Worth correcting; not an emergency. |
| 🔵 **low** | Minor / informational. |
| ⚪ **info** | Nothing wrong detected. |

Groups and users with **critical** or **high** items expand automatically; quieter ones stay collapsed. Top chips jump you to a group.

# What flags mean 

| Flag | Severity | What it means |
|---|---|---|
| `timeout` | critical | Deadline killed the task — ran too long. |
| `stuck_progress` | critical | <5 % rendered after 30 min — render not converging. |
| `output_to_data_pfe` | critical | Render is writing EXRs to `V:` (DATA_PFE). Must use `W:` (DATA_FRM). Directly loads the server that crashes. |
| `ram_saturation` | critical | Peak RAM came within 1 GB of the worker's total — machine was about to swap/crash. |
| `long_frame` | high | A single frame took over 1 h 30 — the ceiling. |
| `memory_grower` | high | Karma's memory kept climbing during the render (leak / runaway scene). |
| `usd_load_failed` | high | Husk could not open the USD scene — broken or missing reference. |
| `non_zero_exit` | high | Renderer exited with an error, no more specific cause found. |
| `mipmap_missing` | high / medium | Textures not mipmapped (`UNTILED`). Convert to `.rat` (Karma) / `.tex` (RenderMan). |
| `aov_excess` | medium | More than 20 AOVs — usually more than the shot needs. |
| `wont_fit_32gb` | medium | Peak RAM ≥ 31 GB — can't run on a 32 GB machine. |
| `env_config_error` | medium | Worker had no `HUSK_PATH` — render never started. |
| `usd_dep_heavy` | medium | 200+ USD layers — very heavy scene graph. |
| `wont_fit_16gb` | low | Peak RAM ≥ 15 GB — can't run on a 16 GB machine. |
| `mipmap_offspec` | low | Tiled, but not renderer-native `.rat` / `.tex` format. |
| `render_warnings` | low | Non-fatal warnings (missing primvars, light/displacement notes, etc.). |
