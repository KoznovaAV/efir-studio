"""Resumable acquisition of the two pinned large Windows AI artifacts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1] / ".efir-local" / "ai"
CHUNK = 16 * 1024 * 1024
OLLAMA_ARCHIVE_SHA256 = "0114b9f103d783b96f14390cf7f11d0fa360040b3ee9941f370fdee503cca308"
QWEN_SHA256 = "3e4cb14174460404e7a233e531675303b2fbf7749c02f91864fe311ab6344e4f"
ARTIFACTS = {
    "ollama": (
        "https://github.com/ollama/ollama/releases/download/v0.40.19/ollama-windows-amd64.zip",
        ROOT / "downloads" / "ollama-v0.40.19-windows-amd64.zip", 1718318809, OLLAMA_ARCHIVE_SHA256,
    ),
    "qwen": (
        f"https://registry.ollama.ai/v2/library/qwen3/blobs/sha256:{QWEN_SHA256}",
        ROOT / "models" / "blobs" / f"sha256-{QWEN_SHA256}", 2497280480, QWEN_SHA256,
    ),
}


def verified(path, total, checksum):
    if not path.is_file() or path.stat().st_size != total:
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == checksum


def fetch_archive(url, target, total, checksum):
    partial = target.with_name(target.name + ".download")
    if verified(partial, total, checksum):
        partial.replace(target)
        return
    print("ollama: downloading the official Windows archive", flush=True)
    subprocess.run([
        "curl.exe", "--fail", "--location", "--silent", "--show-error", "--retry", "2",
        "--connect-timeout", "20", "--speed-limit", "1024", "--speed-time", "300",
        "--continue-at", "-", "--output", str(partial), url,
    ], check=True)
    if not verified(partial, total, checksum):
        raise RuntimeError("ollama: archive SHA-256 mismatch")
    partial.replace(target)


def fetch(name, workers):
    url, target, total, checksum = ARTIFACTS[name]
    target.parent.mkdir(parents=True, exist_ok=True)
    if verified(target, total, checksum):
        print(f"{name}: verified existing artifact ({total} bytes)", flush=True)
        return
    if name == "ollama":
        fetch_archive(url, target, total, checksum)
        print(f"{name}: verified {total} bytes, SHA-256 {checksum}", flush=True)
        return
    parts = target.with_name(target.name + ".parts")
    parts.mkdir(exist_ok=True)

    def download(offset):
        end = min(offset + CHUNK, total) - 1
        count = end - offset + 1
        part = parts / str(offset)
        if part.is_file() and part.stat().st_size == count:
            return part
        temporary = part.with_suffix(".download")
        expected = f"206 bytes {offset}-{end}/{total}"
        for _ in range(3):
            result = subprocess.run([
                "curl.exe", "--silent", "--show-error", "--fail", "--location",
                "--connect-timeout", "15", "--max-time", "120", "--max-filesize", str(count),
                "--range", f"{offset}-{end}", "--output", str(temporary),
                "--write-out", "%{http_code} %header{content-range}", url,
            ], capture_output=True, text=True, check=False)
            if (result.returncode == 0 and result.stdout.strip() == expected
                    and temporary.stat().st_size == count):
                temporary.replace(part)
                return part
        raise RuntimeError(f"{name}: range {offset}-{end} unavailable (curl {result.returncode})")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        downloaded = []
        for index, part in enumerate(pool.map(download, range(0, total, CHUNK)), 1):
            downloaded.append(part)
            if index % 8 == 0:
                print(f"{name}: {min(index * CHUNK, total) / 2**20:.0f}/{total / 2**20:.0f} MiB", flush=True)
    combined = target.with_name(target.name + ".verified-pending")
    try:
        with combined.open("wb") as destination:
            for part in downloaded:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
        if not verified(combined, total, checksum):
            raise RuntimeError(f"{name}: artifact SHA-256 mismatch")
        combined.replace(target)
    finally:
        combined.unlink(missing_ok=True)
    for part in downloaded:
        part.unlink()
    parts.rmdir()
    print(f"{name}: verified {total} bytes, SHA-256 {checksum}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", choices=ARTIFACTS)
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    fetch(args.artifact, args.workers)


if __name__ == "__main__":
    main()
