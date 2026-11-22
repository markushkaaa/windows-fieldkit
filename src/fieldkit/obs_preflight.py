import argparse
import base64
import getpass
import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fieldkit.common import number_between, run, timestamp, write_json


class ObsSession:
    def __init__(self, connection):
        self.connection = connection

    def identify(self, password=None):
        hello = json.loads(self.connection.recv())
        if hello.get("op") != 0:
            raise ValueError("Unexpected OBS WebSocket greeting.")
        options = {"rpcVersion": 1, "eventSubscriptions": 0}
        if authentication := hello["d"].get("authentication"):
            if password is None:
                password = getpass.getpass("OBS WebSocket password: ")
            secret = base64.b64encode(hashlib.sha256((password + authentication["salt"]).encode()).digest())
            challenge = secret + authentication["challenge"].encode()
            options["authentication"] = base64.b64encode(hashlib.sha256(challenge).digest()).decode()
        self.connection.send(json.dumps({"op": 1, "d": options}))
        if json.loads(self.connection.recv()).get("op") != 2:
            raise ValueError("OBS WebSocket authentication failed.")

    def request(self, name):
        request_id = str(uuid.uuid4())
        self.connection.send(json.dumps({"op": 6, "d": {"requestType": name, "requestId": request_id}}))
        while True:
            reply = json.loads(self.connection.recv())
            payload = reply.get("d", {})
            if reply.get("op") == 7 and payload.get("requestId") == request_id:
                status = payload["requestStatus"]
                if not status["result"]:
                    raise RuntimeError(f"OBS rejected {name}: {status.get('comment', status['code'])}")
                return payload.get("responseData", {})


@contextmanager
def connect_obs(port):
    from websocket import WebSocketException, create_connection
    connection = None
    try:
        connection = create_connection(f"ws://127.0.0.1:{port}", timeout=10,
                                       http_no_proxy=["127.0.0.1"])
        session = ObsSession(connection)
        session.identify()
        yield session
    except WebSocketException as exc:
        raise RuntimeError(f"OBS WebSocket connection failed: {exc}") from exc
    finally:
        if connection:
            connection.close()


def capture(session, seconds, wait=time.sleep):
    if session.request("GetStreamStatus")["outputActive"]:
        raise RuntimeError("Stop the live stream before running preflight.")
    if session.request("GetRecordStatus")["outputActive"]:
        raise RuntimeError("A recording is already in progress.")
    session.request("StartRecord")
    try:
        wait(seconds)
    finally:
        # Stop only a recording we started and that is still running.
        if session.request("GetRecordStatus")["outputActive"]:
            stopped = session.request("StopRecord")
        else:
            stopped = None
    if not stopped:
        raise RuntimeError("OBS stopped recording before the test finished.")
    return Path(stopped["outputPath"])


def ffmpeg_output(binary, source, mapping, filter_flag, filters, limit):
    result = subprocess.run(
        [binary, "-hide_banner", "-nostdin", "-i", str(source.resolve()), "-map", mapping,
         filter_flag, filters, "-t", str(limit), "-f", "null", "-"],
        encoding="utf-8", errors="replace", capture_output=True, timeout=180,
    )
    if result.returncode:
        if "matches no streams" in result.stderr:
            return None
        raise RuntimeError("FFmpeg analysis failed: " + "\n".join(result.stderr.splitlines()[-4:]))
    return result.stderr


def analyze(binary, source, tracks, limit):
    if not source.is_file():
        raise ValueError(f"Recording not found: {source}")
    video = ffmpeg_output(binary, source, "0:v:0", "-vf", "blackdetect=d=0.5:pix_th=0.1", limit)
    black = [
        {"start_s": float(start), "end_s": float(end)}
        for start, end in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", video or "")
    ]
    audio = []
    for track in tracks:
        result = ffmpeg_output(
            binary, source, f"0:a:{track - 1}", "-af", "silencedetect=n=-50dB:d=0.7,volumedetect", limit,
        )
        peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", result or "")
        peak_db = float(peak[1]) if peak else None
        audio.append({
            "track": track, "present": result is not None, "peak_dbfs": peak_db,
            "near_full_scale": peak_db is not None and peak_db >= -0.1,
            "silence_starts_s": [float(t) for t in re.findall(r"silence_start:\s*([\d.]+)", result or "")],
        })
    return {"kind": "obs-preflight", "version": 1, "utc": timestamp(), "analyzed_up_to_s": limit,
            "video_present": video is not None, "black_intervals": black, "audio": audio}


def execute(args):
    binary = shutil.which(args.ffmpeg)
    if not binary:
        raise ValueError("FFmpeg not found. Add it to PATH or pass --ffmpeg <path>.")
    tracks = list(dict.fromkeys(args.track or [1, 2]))
    if len(tracks) > 2:
        raise ValueError("Select at most two audio tracks.")
    source = args.file
    if not source:
        with connect_obs(args.port) as session:
            source = capture(session, args.seconds)
        print(f"Recording retained at {source}")
    report = analyze(binary, source, tracks, args.limit)
    write_json(args.output, report)
    print(f"Video present: {report['video_present']}. Black intervals: {len(report['black_intervals'])}.")
    for track in report["audio"]:
        print(f"Audio {track['track']}: present={track['present']}, peak={track['peak_dbfs']} dBFS, "
              f"silences={len(track['silence_starts_s'])}, near full scale={track['near_full_scale']}")
    print(f"Report: {args.output}")


def main():
    parser = argparse.ArgumentParser(description="Check a local OBS recording for black video and audio issues.")
    parser.add_argument("--file", type=Path, help="Analyze an existing file instead of recording in OBS.")
    parser.add_argument("--port", type=number_between(1, 65535, int), default=4455)
    parser.add_argument("--seconds", type=number_between(2, 60, int), default=8)
    parser.add_argument("--limit", type=number_between(2, 120, int), default=60)
    parser.add_argument("--track", action="append", type=number_between(1, 20, int))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--output", type=Path, default=Path("obs-preflight.json"))
    run(parser, execute)


if __name__ == "__main__":
    main()
