# SysWatch tab adaptation plan

## Research summary

SysWatch is a read-only terminal diagnostics TUI for Linux and macOS. Its full view has twelve subsystem tabs, plus Lite and Dense layouts. It keeps a rolling sample history, supports session scrubbing and recording/replay, and presents heuristic anomaly cards with suggested investigation tabs. The README describes each tab's main role; the reference documents controls and dense/lite layouts.

| SysWatch tab | What it presents |
| --- | --- |
| Overview | Activity across CPU, memory, disks, processes, GPU, power, and network. |
| CPU | Total and per-core use, load, temperature, and Linux PSI stalls where available. |
| Memory | RAM, swap, memory pressure, and process memory details. |
| Disks | Aggregate read/write throughput and I/O pressure. |
| Filesystems | Mounts, capacity, free space, and inode use. |
| Procs | Process resource use with sorting, filtering, and row detail. |
| GPU | Device utilization and memory on supported hardware. |
| Power | Battery, power draw, fans, and thermal readings where permissions and sensors allow. |
| Services | systemd or launchd services with state and filtering. |
| Net | Network activity and traffic history. |
| Timeline | Session activity and synchronized history scrubbing. |
| Insights | Heuristics for swap thrash, runaway processes, disk-full conditions, memory pressure, high load, and zombies, linked to relevant tabs. |

The Lite layout targets one 80×24 screen. Dense tiles CPU, memory, network, cores, disk, and processes on a larger screen. `V` cycles layouts. `←`/`→` and `Home`/`End` scrub samples; `R` records, `S` saves a snapshot, and `--replay` opens a recording. SysWatch is read-only and does not require sudo; unsupported measurements are reported as unavailable.

Primary references: [SysWatch README](https://github.com/matthart1983/syswatch#readme) and [SysWatch reference](https://github.com/matthart1983/syswatch/blob/main/docs/REFERENCE.md).

Visual references: [btop](https://github.com/aristocratos/btop) pairs live subsystem graphs with device/process details; [abtop](https://github.com/RussPalms/abtop_dev) emphasizes session selection, context gauges, child processes, and quota panels.

## Envitop mapping

| Envitop tab | Adapted content |
| --- | --- |
| Overview | Keep Envitop's existing three-column dashboard as the default. With one GPU, preserve its current nvitop panel. With multiple GPUs, show a pooled cluster summary and additive per-device-colored utilization/VRAM histories. |
| CPU | Two-column core utilization bars with per-core temperatures and labels. The right side shows split user/total Braille history and top CPU processes. Header stats include CPU/user percentages, load, task and thread counts. |
| GPU | GPU-only view with pooled cluster utilization/VRAM histories, per-device telemetry and colors, driver/CUDA details, and GPU process columns for host CPU/RAM, SM, GMBW, VRAM, runtime, command, and project detail. A single GPU retains the current nvitop presentation. |
| Net | Smaller RX/TX Braille history followed by interface blocks with current rates, OS-reported link maximum, and process tables. Linux TCP byte counters provide per-process RX/s and TX/s when `ss -tinp` exposes them; unsupported values remain unavailable. |
| Memory | One combined page for the three SysWatch memory-related areas: RAM/swap and cached memory, aggregate/device disk I/O, mounted filesystem type/capacity/free space, and inode use. |
| Procs | Unified sortable process list: host processes plus GPU-only PIDs, with host CPU/memory/RSS and GPU index, type, SM%, GMBW%, VRAM, and runtime columns. GPU metrics join to host rows by PID. `/` and `f` search across host fields and GPU command/device fields. |
| Agents | abtop-inspired session table with AI, PID, project, session, summary, state, model, context use, tokens, memory, and turns. The selected session adds working directory/git branch, task, children/subagents, ports, recent tools/chat, token/cache breakdown, and quota. |

The navigation remains seven tabs: Overview, CPU, GPU, Net, Memory, Procs, and Agents. Timeline scrubbing stays on the existing global history keys. Insights remain measurement-backed status indicators rather than tabs. Power and Services are omitted because Envitop has no corresponding collectors. Per-process network byte rates are shown only when the local Linux socket interface makes them available.

### GPU aggregation rules

- Cluster utilization is the unweighted mean across detected devices. Each device contributes `utilization / device_count` percentage points to the stacked history, so one GPU at 100% of two contributes half the graph height.
- Pooled VRAM percentage is `sum(used bytes) / sum(total bytes)`. Each device's history contribution is weighted by its share of total VRAM capacity.
- Aggregate power draw and power limit are summed. The displayed temperature is the hottest reported device temperature.
- Device colors follow GPU index order and remain stable across utilization and VRAM graphs. The focused GPU page omits CPU metrics and retains per-device rows for diagnosis.

## Implementation sequence

1. Add a top-level tab bar and make Overview the startup tab while preserving its current panels and nvitop presentation.
2. Redesign CPU, GPU, Net, Memory, Procs, and Agents for their resource-specific detail. Keep the existing Overview composition and default nvitop overview.
3. Merge GPU process telemetry into Procs by PID, retain GPU-only PIDs, show both host and GPU columns, and support interactive search/filtering.
4. Preserve global Timeline scrubbing and show diagnostic insights in the tab bar without adding tabs.
5. Use Envitop's fixed Braille graph renderer for all charts; do not port SysWatch graph code.
6. Update help, footer controls, and README navigation/feature descriptions.

## Checks and acceptance

- Review the seven-tab mapping, keyboard navigation, Overview default, and responsive layouts.
- Confirm Memory combines all three SysWatch areas and shows inode data when the OS reports it.
- Confirm GPU command, Net per-interface process tables, and Procs filtering preserve unavailable-data semantics.
- Confirm Agents expose the useful session, token, context, child, port, and activity details available from local sources.
- Use static syntax/lint checks; do not treat synthetic demo data as live collector or GPU validation.
