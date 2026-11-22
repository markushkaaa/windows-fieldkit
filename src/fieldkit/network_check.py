import argparse
import csv
import ctypes
import hmac
import ipaddress
import json
import math
import re
import secrets
import socket
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

from fieldkit.common import number_between, read_report, run, timestamp, write_json


DEVICE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,30}")
MAX_BODY = 1024 * 1024


def ipv4(host):
    address = ipaddress.IPv4Address(socket.gethostbyname(host))
    if address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
        raise ValueError("Use a unicast IPv4 address.")
    return str(address)


class WindowsPing:
    def __init__(self):
        if not hasattr(ctypes, "WinDLL"):
            raise RuntimeError("The ICMP probe requires Windows.")
        self.api = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.api.IcmpCreateFile.restype = wintypes.HANDLE
        self.api.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        self.api.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.WORD, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.DWORD, wintypes.DWORD]
        self.api.IcmpSendEcho.restype = wintypes.DWORD

    def __call__(self, address):
        handle = self.api.IcmpCreateFile()
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            request = ctypes.create_string_buffer(b"fieldkit")
            response = ctypes.create_string_buffer(128)
            destination = int.from_bytes(socket.inet_aton(address), "little")
            replies = self.api.IcmpSendEcho(handle, destination, request, len(request), None,
                                            response, len(response), 750)
            if not replies:
                return None
            _, status, milliseconds = (wintypes.DWORD * 3).from_buffer(response)
            return milliseconds if status == 0 else None
        finally:
            self.api.IcmpCloseHandle(handle)


def require_device(device):
    if not isinstance(device, str) or not DEVICE.fullmatch(device):
        raise ValueError("Device name must be 1 to 31 ASCII letters, digits, dashes or underscores.")
    return device


def validate_report(report):
    if not isinstance(report, dict) or report.get("kind") != "network-check" or report.get("version") != 1:
        raise ValueError("Expected a network-check v1 report.")
    require_device(report["device"])
    for name in ("gateway", "external"):
        ipaddress.IPv4Address(report["targets"][name])
    samples = report["samples"]
    if not isinstance(samples, list) or not 1 <= len(samples) <= 601:
        raise ValueError("Expected 1 to 601 network samples.")
    for row in samples:
        start = row["epoch_s"]
        if type(start) not in (float, int) or not math.isfinite(start):
            raise ValueError("Invalid timestamp.")
        for key in ("gateway_ms", "external_ms"):
            value = row[key]
            if value is not None and (type(value) not in (float, int) or not 0 <= value <= 2000):
                raise ValueError("Invalid ICMP round-trip time.")
    return report


def collector(bind, port, folder, token, window):
    folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + window

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def log_message(self, *args):
            pass

        def send_json(self, status, data):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            received = self.headers.get("Authorization", "").encode("utf-8")
            if hmac.compare_digest(received, ("Bearer " + token).encode()):
                return True
            self.send_json(403, {"error": "Wrong session token."})
            return False

        def do_GET(self):
            if self.authorized():
                if self.path == "/clock":
                    self.send_json(200, {"epoch_s": time.time()})
                else:
                    self.send_json(404, {"error": "Unknown route."})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/reports":
                self.send_json(404, {"error": "Unknown route."})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_BODY:
                    raise ValueError("Wrong request size.")
                report = validate_report(json.loads(self.rfile.read(length)))
                destination = folder / f"{report['device']}.json"
                if destination.exists():
                    self.send_json(409, {"error": "Use a new device name or session folder."})
                    return
                write_json(destination, report)
            except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
                self.send_json(400, {"error": "Invalid report."})
                return
            self.send_json(201, {"ok": True})
            print(f"Received {destination.name}", flush=True)

    with HTTPServer((bind, port), Handler) as server:
        server.timeout = 0.5
        print(f"Collector listening at {bind}:{server.server_port} for {window} seconds.", flush=True)
        print(f"Session token: {token}", flush=True)
        while time.monotonic() < deadline:
            server.handle_request()


def request_json(server, token, route, data=None):
    url = urlsplit(server)
    if url.scheme != "http" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("Collector URL must be http://LAN-IP:port.")
    if not ipaddress.IPv4Address(url.hostname).is_private:
        raise ValueError("Use a private or loopback IPv4 address for the collector.")
    request = urllib.request.Request(
        server.rstrip("/") + route, json.dumps(data).encode() if data is not None else None,
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.loads(response.read(MAX_BODY + 1))


def align_clock(request):
    attempts = []
    for _ in range(3):
        started = time.time()
        server = request("/clock")["epoch_s"]
        finished = time.time()
        attempts.append((finished - started, server - (started + finished) / 2))
    return min(attempts, key=lambda attempt: attempt[0])


def collect(device, targets, seconds, offset, round_trip, ping):
    require_device(device)
    started = time.monotonic()
    epoch = time.time() + offset
    samples = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for tick in range(seconds):
            time.sleep(max(0, started + tick - time.monotonic()))
            elapsed = time.monotonic() - started
            gateway = executor.submit(ping, targets["gateway"])
            external = executor.submit(ping, targets["external"])
            samples.append({"epoch_s": round(epoch + elapsed, 4), "gateway_ms": gateway.result(),
                            "external_ms": external.result()})
    return {"kind": "network-check", "version": 1, "device": device, "targets": targets,
            "clock_round_trip_ms": round(round_trip * 1000, 2), "samples": samples}


def nearest_pairs(first, second, tolerance=0.6):
    i = 0
    j = 0
    while i < len(first) and j < len(second):
        a = first[i]
        b = second[j]
        delta = a["epoch_s"] - b["epoch_s"]
        if abs(delta) <= tolerance:
            yield a, b
            i += 1
            j += 1
        elif delta < 0:
            i += 1
        else:
            j += 1


def compare(first, second, output):
    if first["targets"] != second["targets"] or first["device"] == second["device"]:
        raise ValueError("Choose reports from two different devices probing the same addresses.")
    pairs = list(nearest_pairs(first["samples"], second["samples"]))
    if not pairs:
        raise ValueError("The runs do not overlap. Start both devices around the same time.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        csv_writer = csv.writer(handle)
        csv_writer.writerow(["utc", "offset_s", "a_gateway_ms", "b_gateway_ms", "a_external_ms", "b_external_ms"])
        for a, b in pairs:
            csv_writer.writerow([timestamp(a["epoch_s"]), round(b["epoch_s"] - a["epoch_s"], 3),
                                 a["gateway_ms"], b["gateway_ms"], a["external_ms"], b["external_ms"]])
    print(f"{len(pairs)} paired samples from {first['device']} and {second['device']}.")
    for target in ("gateway_ms", "external_ms"):
        both = sum(a[target] is None and b[target] is None for a, b in pairs)
        one = sum((a[target] is None) != (b[target] is None) for a, b in pairs)
        medians = [median(values) if values else None
                   for values in ([pair[i][target] for pair in pairs if pair[i][target] is not None] for i in (0, 1))]
        print(f"{target}: no reply on both={both}, only one={one}, median A/B={medians}")
    print(f"Timeline: {output}")


def execute(args):
    if args.action == "serve":
        address = ipaddress.IPv4Address(args.bind)
        if not address.is_private or address.is_unspecified:
            raise ValueError("Bind to an explicit private LAN or loopback address.")
        collector(str(address), args.port, args.folder, "fk-" + secrets.token_urlsafe(18), args.seconds)
    elif args.action == "join":
        require_device(args.device)
        targets = {"gateway": ipv4(args.gateway), "external": ipv4(args.external)}
        def request(route, data=None):
            return request_json(args.server, args.token, route, data)
        round_trip, offset = align_clock(request)
        if round_trip > 0.5:
            print("Warning: collector clock alignment may be inaccurate.")
        report = collect(args.device, targets, args.seconds, offset, round_trip, WindowsPing())
        write_json(args.output, report)
        print(f"Local report: {args.output}")
        request("/reports", report)
        print("Report sent to the collector.")
    else:
        first = validate_report(read_report(args.first, "network-check"))
        second = validate_report(read_report(args.second, "network-check"))
        compare(first, second, args.output)


def main():
    parser = argparse.ArgumentParser(description="Compare synchronized IPv4 ping samples from two Windows PCs.")
    actions = parser.add_subparsers(dest="action", required=True)
    server = actions.add_parser("serve")
    server.add_argument("--bind", default="127.0.0.1")
    server.add_argument("--port", type=number_between(1024, 65535, int), default=8765)
    server.add_argument("--seconds", type=number_between(10, 1800, int), default=300)
    server.add_argument("--folder", type=Path, default=Path("network-reports"))
    client = actions.add_parser("join")
    client.add_argument("server")
    client.add_argument("--token", required=True)
    client.add_argument("--device", required=True)
    client.add_argument("--gateway", required=True)
    client.add_argument("--external", default="1.1.1.1")
    client.add_argument("--seconds", type=number_between(2, 600, int), default=60)
    client.add_argument("--output", type=Path, default=Path("network-local.json"))
    summary = actions.add_parser("compare")
    summary.add_argument("first", type=Path)
    summary.add_argument("second", type=Path)
    summary.add_argument("--output", type=Path, default=Path("network-timeline.csv"))
    run(parser, execute)


if __name__ == "__main__":
    main()
