import argparse
import ctypes
import threading
import time
from ctypes import wintypes
from pathlib import Path

from fieldkit.common import number_between, read_report, run, timestamp, write_json


BLOCK_BYTES = 1024 * 1024


def volume_id(root):
    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("Volume monitoring requires Windows.")
    get_name = ctypes.WinDLL("kernel32", use_last_error=True).GetVolumeNameForVolumeMountPointW
    get_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_name.restype = wintypes.BOOL
    name = ctypes.create_unicode_buffer(256)
    return name.value if get_name(str(root), name, len(name)) else None


def run_probe(source, seconds, interval, rate, identify=volume_id):
    root = Path(source.anchor)
    identity = identify(root)
    if not identity:
        raise ValueError("The selected path must be on a mounted local volume.")
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Select an existing non-empty file on the drive.")
    if source.is_symlink():
        raise ValueError("Select a regular file on the drive.")
    file_size = source.stat().st_size
    started = time.monotonic()
    started_utc = timestamp()
    stopped = threading.Event()
    lock = threading.Lock()
    counters = {"bytes_read": 0, "read_errors": 0, "last_read_error": None}

    def read_blocks():
        while not stopped.is_set():
            current = identify(root)
            if current != identity:
                stopped.wait(interval)
                continue
            try:
                with source.open("rb", buffering=0) as file:
                    while not stopped.is_set() and identify(root) == identity:
                        block_started = time.monotonic()
                        block = file.read(BLOCK_BYTES)
                        if not block:
                            file.seek(0)
                            continue
                        with lock:
                            counters["bytes_read"] += len(block)
                        delay = len(block) / rate - (time.monotonic() - block_started)
                        stopped.wait(max(0, delay))
            except OSError as exc:
                with lock:
                    counters["read_errors"] += 1
                    counters["last_read_error"] = exc.winerror if hasattr(exc, "winerror") else exc.errno
                stopped.wait(interval)

    events = []
    previous = "present"
    reader = threading.Thread(target=read_blocks, daemon=True)
    reader.start()
    try:
        deadline = started + seconds
        while time.monotonic() < deadline:
            found = identify(root)
            status = "present" if found == identity else "missing" if not found else "different_volume"
            if status != previous:
                events.append({"elapsed_s": round(time.monotonic() - started, 3), "state": status})
                previous = status
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    finally:
        stopped.set()
        reader.join(timeout=1)
    with lock:
        result = counters.copy()
    result.update({"kind": "usb-check", "version": 1, "utc": started_utc, "duration_s": seconds,
                   "poll_s": interval, "read_limit_mib_s": rate / BLOCK_BYTES, "file_size": file_size,
                   "events": events, "read_still_pending": reader.is_alive()})
    return result


def summary(report):
    return {"observed_disconnects": sum(event["state"] == "missing" for event in report["events"]),
            "different_volumes": sum(event["state"] == "different_volume" for event in report["events"]),
            "read_errors": report["read_errors"], "mib_read": round(report["bytes_read"] / BLOCK_BYTES, 2),
            "read_still_pending": report["read_still_pending"]}


def execute(args):
    if args.action == "run":
        original_root = args.file.absolute().anchor
        source = args.file.resolve(strict=True)
        if source.anchor.lower() != original_root.lower():
            raise ValueError("Choose a file directly on the target volume.")
        report = run_probe(source, args.seconds, args.poll, args.rate * BLOCK_BYTES)
        write_json(args.output, report)
        print(summary(report))
    else:
        before = read_report(args.before, "usb-check")
        after = read_report(args.after, "usb-check")
        settings = ("duration_s", "poll_s", "read_limit_mib_s", "file_size")
        if any(before[key] != after[key] for key in settings):
            raise ValueError("Compare runs with the same duration, polling, rate, and test-file size.")
        result = {"before": summary(before), "after": summary(after)}
        print(result)
        if args.output:
            write_json(args.output, result)


def main():
    parser = argparse.ArgumentParser(description="Record mounted-volume interruptions during a read-only test.")
    actions = parser.add_subparsers(dest="action", required=True)
    probe = actions.add_parser("run")
    probe.add_argument("file", type=Path, help="An existing large file on the external SSD.")
    probe.add_argument("--seconds", type=number_between(2, 3600, int), default=60)
    probe.add_argument("--poll", type=number_between(0.05, 2), default=0.2)
    probe.add_argument("--rate", type=number_between(1, 256, int), default=16, help="Maximum MiB/s.")
    probe.add_argument("--output", type=Path, default=Path("usb-check.json"))
    compare = actions.add_parser("compare")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--output", type=Path)
    run(parser, execute)


if __name__ == "__main__":
    main()
