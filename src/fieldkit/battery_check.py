import argparse
import ctypes
import hashlib
import threading
import time
from ctypes import wintypes
from pathlib import Path
from statistics import median

from fieldkit.common import number_between, read_report, run, timestamp, write_json


class BatteryState(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("present", ctypes.c_ubyte), ("charging", ctypes.c_ubyte),
                ("discharging", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte * 4),
                ("maximum", wintypes.DWORD), ("remaining", wintypes.DWORD), ("rate", wintypes.LONG),
                ("estimated_seconds", wintypes.DWORD), ("alert1", wintypes.DWORD), ("alert2", wintypes.DWORD)]


class PowerStatus(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("battery_flag", ctypes.c_ubyte),
                ("percent", ctypes.c_ubyte), ("system_status", ctypes.c_ubyte),
                ("life", wintypes.DWORD), ("full_life", wintypes.DWORD)]


def battery_sample():
    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("The battery test requires Windows.")
    status = PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise RuntimeError("Windows could not read system power status.")
    if status.battery_flag == 128 or status.percent == 255 or status.ac == 255:
        raise RuntimeError("A system battery with readable charge and AC status is required.")
    state = BatteryState()
    status_code = ctypes.windll.powrprof.CallNtPowerInformation(
        5, None, 0, ctypes.byref(state), ctypes.sizeof(state)
    )
    detailed = status_code == 0 and state.present
    return {"ac": status.ac == 1, "percent": status.percent,
            "remaining_mwh": state.remaining if detailed and state.remaining != 0xFFFFFFFF else None,
            "rate_mw": state.rate if detailed and state.rate < 0 and state.rate != -2147483648 else None}


def measure(seconds, interval, workload, sample=battery_sample):
    initial = sample()
    if initial["ac"] or initial["percent"] <= 15:
        raise ValueError("Unplug AC power and start with more than 15% charge.")
    finished = threading.Event()
    payload = bytes(1024 * 1024)
    iterations = 0

    def work():
        nonlocal iterations
        while not finished.is_set():
            hashlib.sha256(payload).digest()
            iterations += 1

    worker = threading.Thread(target=work, daemon=True)
    started_utc = timestamp()
    started = time.monotonic()
    deadline = started + seconds
    readings = [{"elapsed_s": 0, **initial}]
    reason = "completed"
    if workload == "cpu":
        worker.start()
    try:
        while time.monotonic() < deadline:
            time.sleep(max(0, min(interval, deadline - time.monotonic())))
            elapsed = round(time.monotonic() - started, 3)
            current = sample()
            if current["ac"] or current["percent"] <= 15:
                reason = "ac_connected" if current["ac"] else "low_battery"
                break
            if elapsed - readings[-1]["elapsed_s"] > interval * 2 + 1:
                reason = "sample_gap"
                break
            readings.append({"elapsed_s": elapsed, **current})
    except KeyboardInterrupt:
        reason = "cancelled"
    finally:
        finished.set()
        if workload == "cpu":
            worker.join()
    return {"kind": "battery-check", "version": 1, "utc": started_utc, "target_s": seconds,
            "interval_s": interval, "workload": workload, "completion": reason, "samples": readings,
            "sha256_mib_processed": iterations if workload == "cpu" else None}


def summary(report):
    first, last = report["samples"][0], report["samples"][-1]
    consumed = None
    if first["remaining_mwh"] is not None and last["remaining_mwh"] is not None:
        delta = first["remaining_mwh"] - last["remaining_mwh"]
        if delta >= 0:
            consumed = delta
    rates = [-reading["rate_mw"] for reading in report["samples"] if reading["rate_mw"] is not None]
    return {"duration_s": last["elapsed_s"], "start_percent": first["percent"],
            "drop_percentage_points": first["percent"] - last["percent"], "capacity_drop_mwh": consumed,
            "median_reported_drain_mw": median(rates) if rates else None, "completion": report["completion"]}


def execute(args):
    if args.action == "run":
        report = measure(args.seconds, args.interval, args.workload)
        write_json(args.output, report)
        print(summary(report))
    else:
        before = read_report(args.before, "battery-check")
        after = read_report(args.after, "battery-check")
        if any(before[key] != after[key] for key in ("target_s", "interval_s", "workload")):
            raise ValueError("Use the same duration, sample interval, and workload.")
        if any(report["completion"] != "completed" for report in [before, after]):
            raise ValueError("Both runs must finish before they can be compared.")
        result = {"before": summary(before), "after": summary(after)}
        print(result)
        if args.output:
            write_json(args.output, result)


def main():
    parser = argparse.ArgumentParser(description="Measure laptop battery drain under a repeatable workload.")
    actions = parser.add_subparsers(dest="action", required=True)
    probe = actions.add_parser("run")
    probe.add_argument("--seconds", type=number_between(10, 7200, int), default=300)
    probe.add_argument("--interval", type=number_between(1, 60, int), default=5)
    probe.add_argument("--workload", choices=["idle", "cpu"], default="idle")
    probe.add_argument("--output", type=Path, default=Path("battery-check.json"))
    compare = actions.add_parser("compare")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--output", type=Path)
    run(parser, execute)


if __name__ == "__main__":
    main()
