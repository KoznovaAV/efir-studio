"""Bounded CLI workloads measured on the process tree with psutil."""

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

import psutil

CLI = [sys.executable, "-X", "utf8", "-m", "efir"]
CACHE_COUNTS = re.compile(r"Кодирование: (\d+); из кэша: (\d+)")
SAMPLE_SECS = 0.05
MIB = 1024 ** 2


def command(args):
    return subprocess.run([*map(str, args)], check=True, capture_output=True,
                          encoding="utf-8", timeout=120).stdout


def fixture(root, ffmpeg):
    files = []
    for index, color in enumerate(("navy", "red", "green", "white")):
        path = root / f"take-{index}.mp4"
        command([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                 f"testsrc2=s=240x160:r=25:d=4,drawbox=color={color}:t=6",
                 "-c:v", "libx264", "-threads:v", 2, path])
        files.append(path)
    return files


def make_project(root, name, clips, scene_count, base_frames):
    project = root / name
    command([*CLI, "init", project, "--title", name])
    total_frames = 0
    for index in range(scene_count):
        frames = base_frames + index // len(clips)
        total_frames += frames
        command([*CLI, "add", project, "--title", f"Сцена {index + 1}",
                 "--visual", clips[index % len(clips)], "--duration", f"{frames / 25:.2f}",
                 "--caption", f"Кадр {index + 1}"])
    return project, total_frames


def process_sample(roots, cpu):
    memory = 0
    for root in roots:
        try:
            family = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            continue
        for child in family:
            try:
                identity = (child.pid, child.create_time())
                times = child.cpu_times()
                total = times.user + times.system
                memory += child.memory_info().rss
                cpu[identity] = max(total, cpu.get(identity, 0))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    return memory


def measure(root, label, project, ffmpeg, scene_count, frames, workers=1):
    targets = [root / f"{label}-{index}.mp4" for index in range(workers)]
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=root) as stage:
        handles, processes, cpu = [], [], {}
        try:
            for index, target in enumerate(targets):
                output = open(Path(stage) / f"{index}.log", "w+b")
                handles.append(output)
                processes.append(psutil.Popen(
                    [*CLI, "export", str(project), str(target), "--draft", "--overwrite", "--ffmpeg", ffmpeg],
                    stdout=output, stderr=subprocess.STDOUT,
                ))
            peak = 0
            deadline = started + 120
            while True:
                peak = max(peak, process_sample(processes, cpu))
                if all(process.poll() is not None for process in processes):
                    break
                if time.perf_counter() >= deadline:
                    raise TimeoutError(f"{label}: 120 seconds exceeded")
                time.sleep(SAMPLE_SECS)
            elapsed = time.perf_counter() - started
            counts = []
            for output, process in zip(handles, processes):
                output.seek(0)
                log = output.read().decode("utf-8", errors="replace")
                match = CACHE_COUNTS.search(log)
                if process.returncode or not match:
                    raise RuntimeError(log)
                counts.append(tuple(map(int, match.groups())))
        finally:
            for process in processes:
                if process.poll() is None:
                    for child in reversed(process.children(recursive=True)):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    process.kill()
                    process.wait(timeout=5)
            for handle in handles:
                handle.close()

    for target, count in zip(targets, counts):
        crc = command([ffmpeg, "-v", "error", "-i", target, "-map", "0:v:0", "-c:v", "copy", "-f", "framecrc", "-"])
        if sum(line[:1].isdigit() for line in crc.splitlines()) != frames or sum(count) != scene_count:
            raise RuntimeError(f"{label}: wrong frame or cache count")
    cache = list((project / ".cache").glob("*/*.mkv"))
    cpu_seconds = sum(cpu.values())
    return {
        "case": label, "scenes": scene_count, "duration_sec": frames / 25, "workers": workers,
        "wall_sec": round(elapsed, 3), "peak_tree_rss_mib": round(peak / MIB, 2),
        "sampled_cpu_sec": round(cpu_seconds, 3),
        "mean_host_capacity_pct": round(100 * cpu_seconds / elapsed / psutil.cpu_count(), 2),
        "encoded": sum(count[0] for count in counts), "reused": sum(count[1] for count in counts),
        "cache_files": len(cache), "cache_bytes": sum(path.stat().st_size for path in cache),
        "output_bytes": sum(path.stat().st_size for path in targets),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/qa/load"))
    parser.add_argument("--soak-seconds", type=int, default=60, choices=range(15, 121), metavar="15–120")
    parser.add_argument("--case", choices=("small", "many", "long", "parallel"), help="run one workload")
    args = parser.parse_args()
    ffmpeg = shutil.which(os.environ.get("EFIR_FFMPEG") or "ffmpeg")
    if not ffmpeg:
        parser.error("Set EFIR_FFMPEG or install FFmpeg")
    args.output.mkdir(parents=True, exist_ok=True)
    if psutil.virtual_memory().available < 2048 * MIB or shutil.disk_usage(args.output).free < 512 * MIB:
        parser.error("The workload needs at least 2 GiB available RAM and 512 MiB free disk")
    details = {
        "started_utc": datetime.now(timezone.utc).isoformat(), "os": platform.platform(),
        "processor": platform.processor(), "logical_cpus": psutil.cpu_count(),
        "total_ram_mib": round(psutil.virtual_memory().total / MIB),
        "python": platform.python_version(), "psutil": psutil.__version__,
        "ffmpeg": command([ffmpeg, "-version"]).splitlines()[0],
        "resolution": "540x960", "fps": 25, "sample_interval_sec": SAMPLE_SECS,
        "requested_soak_sec": args.soak_seconds,
        "selected_case": args.case or "all",
    }
    rows = []
    with tempfile.TemporaryDirectory(prefix="EFIR load ", dir=args.output.resolve()) as temp:
        root = Path(temp)
        clips = fixture(root, ffmpeg)
        for name, count, base in (("small", 6, 25), ("many", 18, 25), ("long", 30, 76), ("parallel", 12, 38)):
            if args.case and name != args.case:
                continue
            print(f"Measuring {name}: {count} scenes", flush=True)
            project, frames = make_project(root, name, clips, count, base)
            if name == "parallel":
                rows.append(measure(root, name, project, ffmpeg, count, frames, workers=2))
                continue
            for state in ("cold", "warm"):
                rows.append(measure(root, f"{name}-{state}", project, ffmpeg, count, frames))
            if name == "long":
                start = time.perf_counter()
                while time.perf_counter() - start < args.soak_seconds:
                    index = len(rows)
                    rows.append(measure(root, f"soak-{index:02}", project, ffmpeg, count, frames))
                    time.sleep(3)
                details["actual_soak_wall_sec"] = round(time.perf_counter() - start, 3)
    csv_file = args.output / "results.csv"
    with csv_file.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.output / "environment.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(rows)} measured workloads saved to {csv_file}")


if __name__ == "__main__":
    main()
