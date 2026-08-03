#!/usr/bin/env python3
"""Read-only resource audit for the Hermes production server.

The report separates real resident memory (RSS) from virtual memory (VSZ)
because Node/Next processes can reserve huge virtual address ranges without
actually using that much RAM.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
from typing import Iterable


DEFAULT_DATA_DIR = pathlib.Path("/opt/hermes-youyi/data/tuoguan-data")


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", "replace").strip()
    except Exception as exc:  # pragma: no cover - best-effort system report
        return f"ERROR: {exc}"


def human_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def parse_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in pathlib.Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if parts and parts[0].isdigit():
            result[key] = int(parts[0]) * 1024
    return result


def process_rows() -> list[tuple[int, int, int, str]]:
    out = run(["ps", "-eo", "pid=,rss=,vsz=,args="])
    rows: list[tuple[int, int, int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, rss_kb, vsz_kb, args = parts
        try:
            rows.append((int(rss_kb) * 1024, int(vsz_kb) * 1024, int(pid), args))
        except ValueError:
            continue
    return rows


def markdown_table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    header_list = list(headers)
    lines = ["| " + " | ".join(header_list) + " |", "| " + " | ".join(["---"] * len(header_list)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def build_report(data_dir: pathlib.Path) -> str:
    now = dt.datetime.now().astimezone()
    mem = parse_meminfo()
    rows = process_rows()
    total_rss = sum(row[0] for row in rows)
    total_vsz = sum(row[1] for row in rows)
    top_rss = sorted(rows, key=lambda row: row[0], reverse=True)[:12]
    top_vsz = sorted(rows, key=lambda row: row[1], reverse=True)[:8]

    mem_rows = [
        ("物理总内存", human_bytes(mem.get("MemTotal", 0))),
        ("可用内存", human_bytes(mem.get("MemAvailable", 0))),
        ("空闲内存", human_bytes(mem.get("MemFree", 0))),
        ("缓存 Cached", human_bytes(mem.get("Cached", 0))),
        ("可回收内核缓存", human_bytes(mem.get("SReclaimable", 0))),
        ("Swap 总量", human_bytes(mem.get("SwapTotal", 0))),
        ("Swap 可用", human_bytes(mem.get("SwapFree", 0))),
        ("全部进程真实占用 RSS", human_bytes(total_rss)),
        ("全部进程虚拟内存 VSZ", human_bytes(total_vsz)),
    ]

    top_rss_rows = [
        (str(pid), human_bytes(rss), human_bytes(vsz), args[:100])
        for rss, vsz, pid, args in top_rss
    ]
    top_vsz_rows = [
        (str(pid), human_bytes(rss), human_bytes(vsz), args[:100])
        for rss, vsz, pid, args in top_vsz
    ]

    disk = run(["df", "-h", "/"])
    journal = run(["journalctl", "--disk-usage"])
    docker = run(["docker", "system", "df"])
    hermes_service = run(["systemctl", "is-active", "hermes-youyi-019.service"])
    hermes_status = run(["systemctl", "status", "hermes-youyi-019.service", "--no-pager", "--full"])

    return "\n\n".join(
        [
            f"# Hermes Resource Audit\n\n- Generated: {now.isoformat()}\n- Data dir: `{data_dir}`\n- Hermes service: `{hermes_service}`",
            "## Memory\n\n"
            + markdown_table(["Metric", "Value"], mem_rows)
            + "\n\nNote: RSS/RES is real physical memory. VSZ/VIRT is virtual address space and can look very large without meaning real RAM is used.",
            "## Top Real Memory Processes\n\n" + markdown_table(["PID", "RSS", "VSZ", "Command"], top_rss_rows),
            "## Top Virtual Memory Processes\n\n" + markdown_table(["PID", "RSS", "VSZ", "Command"], top_vsz_rows),
            "## Disk\n\n```text\n" + disk + "\n```",
            "## Journal\n\n```text\n" + journal + "\n```",
            "## Docker\n\n```text\n" + docker + "\n```",
            "## Hermes Status\n\n```text\n" + "\n".join(hermes_status.splitlines()[:24]) + "\n```",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a read-only Hermes resource audit report.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--no-write", action="store_true", help="Print only; do not write a report file.")
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir)
    report = build_report(data_dir)
    print(report)
    if not args.no_write:
        reports = data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = reports / f"resource-audit-{stamp}.md"
        target.write_text(report, encoding="utf-8")
        print(f"\nREPORT:{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
