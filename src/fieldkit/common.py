import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile


def timestamp(epoch=None):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else datetime.now(timezone.utc).isoformat()


def number_between(low, high, kind=float):
    def parse(value):
        try:
            result = kind(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Invalid number.") from exc
        if not math.isfinite(result) or not low <= result <= high:
            raise argparse.ArgumentTypeError(f"Expected {low} to {high}.")
        return result
    return parse


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def read_report(path, kind):
    with Path(path).open(encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or data.get("kind") != kind or data.get("version") != 1:
        raise ValueError(f"Expected a {kind} v1 report: {path}")
    return data


def run(parser, action):
    args = parser.parse_args()
    try:
        action(args)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "\nCancelled.\n")
