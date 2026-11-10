# EnviTop tab guide

The dashboard has seven tabs. Press the number shown below to open a view.
The footer highlights the current tab, and the shared header keeps system
status visible as you move between views.

| Key | Tab | What it shows |
| --- | --- | --- |
| `0` | [Overview](overview.md) | Compact view of the whole system |
| `1` | [CPU](cpu.md) | Per-core activity, history, and busiest processes |
| `2` | [GPU](gpu.md) | GPU telemetry and GPU processes |
| `3` | [Net](net.md) | Traffic, interfaces, local services, and listeners |
| `4` | [Memory](memory.md) | RAM, swap, disk activity, and filesystems |
| `5` | [Procs](procs.md) | Host and GPU processes in one table |
| `6` | [Agents](agents.md) | Coding-agent sessions, tokens, quotas, and ports |

Use `j`/`k` or the arrow keys to move through selectable rows. Press `?` for
the complete key reference. Run `./envitop.sh --demo` to explore every tab
with synthetic data; demo mode does not collect host telemetry or allow kills.
