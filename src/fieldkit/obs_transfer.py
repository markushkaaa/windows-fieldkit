import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from fieldkit.common import number_between, run, write_json
from fieldkit.obs_preflight import connect_obs


# OBS source settings that refer to local media. Unknown source settings are
# intentionally dropped because plugins can store arbitrary private fields.
MEDIA_FIELDS = {
    "image_source": "file",
    "ffmpeg_source": "local_file",
    "image_slide_show": "files",
    "slideshow": "files",
    "vlc_source": "playlist",
    "text_gdiplus": "file",
    "text_ft2_source": "text_file",
}
SAFE_FIELDS = {
    "image_source": {"unload"},
    "ffmpeg_source": {"is_local_file", "looping", "restart_on_activate", "close_when_inactive", "hw_decode"},
    "image_slide_show": {"slide_time", "transition", "transition_speed", "loop", "randomize"},
    "slideshow": {"slide_time", "transition", "transition_speed", "loop", "randomize"},
    "vlc_source": {"loop", "shuffle", "playback_behavior"},
    "color_source": {"color", "width", "height"},
    "text_gdiplus": {"read_from_file", "font", "color", "opacity", "align", "valign", "outline", "outline_size"},
    "text_ft2_source": {"from_file", "font", "color1", "color2", "outline", "drop_shadow"},
}
DEVICE_KINDS = {"dshow_input", "wasapi_input_capture", "wasapi_output_capture",
                "wasapi_process_output_capture", "monitor_capture", "window_capture", "game_capture"}
SCENE_FIELDS = {"name", "id", "versioned_id", "uuid", "enabled", "muted", "volume", "mixers", "sync",
                "balance", "flags", "deinterlace_mode", "deinterlace_field_order", "monitoring_type"}
ITEM_FIELDS = {"name", "source_uuid", "id", "visible", "locked", "rot", "pos", "scale", "align", "bounds",
               "bounds_type", "bounds_align", "crop_left", "crop_top", "crop_right", "crop_bottom",
               "scale_filter", "blend_method", "blend_type", "group_item_backup", "bounds_crop",
               "pos_rel", "scale_rel", "scale_ref"}
MAX_ASSET_BYTES = 4 * 1024**3
ASSET_NAME = re.compile(r"assets/[0-9a-f]{64}(?:\.[a-z0-9]{1,12})?")


def source_kind(source):
    return source.get("id") or source.get("versioned_id") or ""


def media_paths(source):
    settings = source["settings"]
    field = MEDIA_FIELDS.get(source_kind(source))
    value = settings.get(field)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("value"), str):
                yield item, "value"
    elif isinstance(value, str) and value:
        yield settings, field


def collection_sources(collection):
    for key in ("sources", "groups"):
        sources = collection.get(key, [])
        if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
            raise ValueError(f"Invalid OBS {key} list.")
        for source in sources:
            yield key, source


def sanitize(collection, archive, base, include_text):
    output = {key: collection[key] for key in ("name", "current_scene", "current_program_scene", "scene_order")
              if key in collection}
    output["sources"] = []
    output["groups"] = []
    checklist = []
    assets = set()
    asset_size = 0
    for group, source in collection_sources(collection):
        kind = source_kind(source)
        name = source.get("name", "(unnamed)")
        cleaned = {key: source[key] for key in SCENE_FIELDS if key in source}
        settings = source.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError(f"Invalid settings for {name}.")
        if kind in {"scene", "group"}:
            items = [{key: item[key] for key in ITEM_FIELDS if key in item} for item in settings.get("items", [])]
            cleaned["settings"] = {"items": items, "id_counter": settings.get("id_counter", 0)}
            for key in ("custom_size", "cx", "cy"):
                if key in settings:
                    cleaned["settings"][key] = settings[key]
        else:
            fields = SAFE_FIELDS.get(kind, set()) | {MEDIA_FIELDS.get(kind)}
            if include_text and kind in {"text_gdiplus", "text_ft2_source"}:
                fields |= {"text"}
            cleaned["settings"] = {key: value for key, value in settings.items() if key in fields}
            media_field = MEDIA_FIELDS.get(kind)
            if isinstance(cleaned["settings"].get(media_field), list):
                cleaned["settings"][media_field] = [
                    {"value": item["value"]} for item in cleaned["settings"][media_field]
                    if isinstance(item, dict) and isinstance(item.get("value"), str)
                ]
            if kind in DEVICE_KINDS:
                checklist.append({"source": name, "kind": kind, "action": "Select the device or window on the new PC."})
            elif kind not in SAFE_FIELDS:
                checklist.append({"source": name, "kind": kind, "action": "Install any required plugin and reconfigure the source."})
        if source.get("filters"):
            checklist.append({"source": name, "kind": kind, "action": "Recreate source filters."})
        for container, key in media_paths(cleaned):
            candidate = Path(container[key])
            path = candidate if candidate.is_absolute() else base / candidate
            container[key] = ""
            if not path.is_file():
                checklist.append({"source": name, "kind": kind, "action": "Select missing media or expand a media folder into files."})
                continue
            size = path.stat().st_size
            if size > MAX_ASSET_BYTES:
                raise ValueError(f"Media file exceeds the 4 GiB limit: {path.name}")
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            extension = path.suffix.lower()
            member = "assets/" + digest + (extension if re.fullmatch(r"\.[a-z0-9]{1,12}", extension) else "")
            if member not in assets:
                asset_size += size
                if asset_size > MAX_ASSET_BYTES:
                    raise ValueError("The selected media exceeds the 4 GiB archive limit.")
                archive.write(path, member)
                assets.add(member)
            container[key] = member
        output[group].append(cleaned)
    return {"kind": "obs-transfer", "version": 1, "collection": output, "checklist": checklist}


def pack(source, output, include_text=False):
    with source.open(encoding="utf-8-sig") as handle:
        original = json.load(handle)
    if not isinstance(original, dict) or not original.get("sources"):
        raise ValueError("Use an OBS Scene Collection JSON export with at least one source.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
        staged = Path(scratch) / "collection.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            bundle = sanitize(original, archive, source.resolve().parent, include_text)
            archive.writestr("manifest.json", json.dumps(bundle, ensure_ascii=False))
        # On Windows rename fails if another file already occupies the path.
        if output.exists():
            raise ValueError(f"Output already exists: {output}")
        staged.rename(output)
    return bundle["checklist"]


def unpack(source, target, available=None):
    target = target.absolute()
    if target.exists():
        raise ValueError("Choose a new output folder. Existing files will not be replaced.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        manifest = archive.getinfo("manifest.json")
        if manifest.file_size > 8 * 1024**2:
            raise ValueError("Archive manifest is too large.")
        bundle = json.loads(archive.read(manifest))
        if bundle.get("kind") != "obs-transfer" or bundle.get("version") != 1:
            raise ValueError("Expected a Fieldkit OBS archive v1.")
        staged = Path(scratch) / "import"
        staged.mkdir()
        copied = set()
        total_size = 0
        checklist = bundle["checklist"]
        for _, item in collection_sources(bundle["collection"]):
            kind = source_kind(item)
            versioned = item.get("versioned_id", kind)
            if available is not None and kind not in {"scene", "group"} and not {kind, versioned} & available:
                checklist.append({"source": item.get("name", "(unnamed)"), "kind": kind,
                                  "action": "This input kind is missing from the running OBS installation."})
            for container, key in media_paths(item):
                member = container[key]
                if not member:
                    continue
                if not ASSET_NAME.fullmatch(member):
                    raise ValueError("Invalid media path in archive.")
                if member not in copied:
                    entry = archive.getinfo(member)
                    total_size += entry.file_size
                    if total_size > MAX_ASSET_BYTES:
                        raise ValueError("Archive exceeds the 4 GiB media limit.")
                    destination = staged / member
                    destination.parent.mkdir(exist_ok=True)
                    with archive.open(entry) as reader, destination.open("wb") as writer:
                        shutil.copyfileobj(reader, writer)
                    with destination.open("rb") as written:
                        actual = hashlib.file_digest(written, "sha256").hexdigest()
                    if actual != destination.stem:
                        raise ValueError("An archived media file failed checksum verification.")
                    copied.add(member)
                container[key] = str(target / member)
        write_json(staged / "collection.json", bundle["collection"])
        write_json(staged / "checklist.json", checklist)
        if target.exists():
            raise ValueError("Output folder already exists.")
        os.rename(staged, target)
    return checklist


def execute(args):
    if args.action == "pack":
        checklist = pack(args.input, args.output, args.include_text)
    else:
        inventory = None
        if args.check_obs:
            with connect_obs(args.port) as session:
                inventory = set(session.request("GetInputKindList")["inputKinds"])
        checklist = unpack(args.input, args.output, inventory)
    print(f"Output: {args.output}. Follow-up items: {len(checklist)}.")
    for item in checklist:
        print(f"- {item['source']}: {item['action']}")


def main():
    parser = argparse.ArgumentParser(description="Move an OBS Scene Collection and its media between Windows PCs.")
    commands = parser.add_subparsers(dest="action", required=True)
    packer = commands.add_parser("pack", help="Pack an exported OBS scene collection.")
    packer.add_argument("input", type=Path)
    packer.add_argument("output", type=Path)
    packer.add_argument("--include-text", action="store_true", help="Include inline OBS text after reviewing it.")
    importer = commands.add_parser("unpack", help="Prepare a new folder for import in OBS.")
    importer.add_argument("input", type=Path)
    importer.add_argument("output", type=Path)
    importer.add_argument("--check-obs", action="store_true", help="Compare source kinds with the receiving OBS.")
    importer.add_argument("--port", type=number_between(1, 65535, int), default=4455)
    run(parser, execute)


if __name__ == "__main__":
    main()
