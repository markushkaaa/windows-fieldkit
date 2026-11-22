import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from fieldkit import battery_check, network_check, obs_preflight, obs_transfer, usb_check


class ObsPreflightTests(unittest.TestCase):
    def test_active_recording_is_untouched(self):
        class Session:
            calls = []

            def request(self, name):
                self.calls.append(name)
                return {"outputActive": name == "GetRecordStatus"}

        session = Session()
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            obs_preflight.capture(session, 0)
        self.assertEqual(session.calls, ["GetStreamStatus", "GetRecordStatus"])

    def test_started_recording_stops_after_interruption(self):
        class Session:
            recording = False

            def request(self, name):
                if name == "StartRecord":
                    self.recording = True
                elif name == "StopRecord":
                    self.recording = False
                return {"outputActive": self.recording}

        def interrupt(_seconds):
            raise KeyboardInterrupt

        session = Session()
        with self.assertRaises(KeyboardInterrupt):
            obs_preflight.capture(session, 0, interrupt)
        self.assertFalse(session.recording)

    @unittest.skipUnless(os.environ.get("FIELDKIT_TEST_FFMPEG"), "Set FIELDKIT_TEST_FFMPEG for media integration test.")
    def test_black_video_silent_audio_and_missing_track(self):
        ffmpeg = os.environ["FIELDKIT_TEST_FFMPEG"]
        with tempfile.TemporaryDirectory() as scratch:
            video = Path(scratch) / "test.mkv"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "color=black:s=64x64:r=10", "-f", "lavfi", "-i", "anullsrc",
                            "-t", "2", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(video)], check=True)
            report = obs_preflight.analyze(ffmpeg, video, [1, 2], 3)
            self.assertTrue(report["video_present"])
            self.assertGreaterEqual(report["black_intervals"][0]["end_s"], 1.8)
            self.assertEqual(report["audio"][0]["silence_starts_s"], [0.0])
            self.assertFalse(report["audio"][1]["present"])


class ObsTransferTests(unittest.TestCase):
    def test_pack_and_restore_media_without_sensitive_settings(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            media = root / "still.png"
            media.write_bytes(b"test asset")
            source = root / "export.json"
            source.write_text(json.dumps({"name": "Demo", "stream_key": "local-only", "sources": [
                {"id": "scene", "name": "Demo", "settings": {"items": [{"name": "Art", "visible": True}]}},
                {"id": "image_source", "name": "Art", "settings": {"file": str(media)}},
                {"id": "browser_source", "name": "Web", "settings": {"url": "https://example.test/?key=private"}},
            ]}), encoding="utf-8")
            bundle = root / "pack.zip"
            target = root / "new-obs"
            obs_transfer.pack(source, bundle)
            with zipfile.ZipFile(bundle) as archive:
                self.assertNotIn(b"private", archive.read("manifest.json"))
                self.assertNotIn(b"local-only", archive.read("manifest.json"))
            checklist = obs_transfer.unpack(bundle, target, {"image_source"})
            result = json.loads((target / "collection.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(result["sources"][1]["settings"]["file"]).read_bytes(), b"test asset")
            self.assertEqual(result["sources"][0]["settings"]["items"][0]["name"], "Art")
            self.assertTrue(any("missing from the running" in item["action"] for item in checklist))
            with self.assertRaises(ValueError):
                obs_transfer.unpack(bundle, target)

    def test_bad_asset_path_cannot_escape_output(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            bundle = root / "pack.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("manifest.json", json.dumps({
                    "kind": "obs-transfer", "version": 1, "checklist": [],
                    "collection": {"sources": [{"id": "image_source", "settings": {"file": "../bad.png"}}]},
                }))
            with self.assertRaisesRegex(ValueError, "Invalid media path"):
                obs_transfer.unpack(bundle, root / "destination")
            self.assertFalse((root / "destination").exists())


class WindowsMeasurementsTests(unittest.TestCase):
    def test_battery_stops_when_ac_is_connected(self):
        readings = iter([{"ac": False, "percent": 80, "remaining_mwh": 40000, "rate_mw": -2000},
                         {"ac": True, "percent": 80, "remaining_mwh": 40000, "rate_mw": None}])
        report = battery_check.measure(0.02, 0.01, "idle", lambda: next(readings))
        self.assertEqual(report["completion"], "ac_connected")
        self.assertEqual(len(report["samples"]), 1)

    def test_usb_read_progress(self):
        with tempfile.TemporaryDirectory() as scratch:
            source = Path(scratch) / "data.bin"
            source.write_bytes(b"x" * 4096)
            report = usb_check.run_probe(source, 0.12, 0.02, 1024**2, lambda _: "volume")
            self.assertGreater(report["bytes_read"], 0)
            self.assertEqual(usb_check.summary(report)["read_errors"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows ICMP API.")
    def test_windows_native_loopback_ping(self):
        self.assertIsNotNone(network_check.WindowsPing()("127.0.0.1"))

    def test_network_alignment_leaves_unmatched_samples_out(self):
        one = [{"epoch_s": 1}, {"epoch_s": 2}, {"epoch_s": 3}]
        two = [{"epoch_s": 1.1}, {"epoch_s": 3.1}]
        self.assertEqual(list(network_check.nearest_pairs(one, two)), [(one[0], two[0]), (one[2], two[1])])


if __name__ == "__main__":
    unittest.main()
