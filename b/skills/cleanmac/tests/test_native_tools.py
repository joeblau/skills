import copy
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zlib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import native_tools
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


class NativeToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = StateStore(self.root / "state")
        self.info = {"protocol_version": 1, "version": "1.0.0", "privileged": False,
                     "capabilities": ["document-versions", "similar-images", "photos", "screen-cleaning"],
                     "image_algorithm": native_tools.ALGORITHM}

    def tearDown(self):
        self.temp.cleanup()

    def dispatch(self, name, action, **options):
        return native_tools.dispatch(name, action, options, store=self.store)

    def photo_record(self, protected=False):
        return native_tools._scan_record(self.store, "photos", {
            "status": "complete", "authorization": "authorized", "items": [{"id": "photo-1", "protected": protected, "modified_at": 1, "selected": False}],
            "deletion_effect": "Whole asset deletion synchronizes through iCloud Photos."})

    def test_registry_covers_four_features(self):
        self.assertEqual({feature for spec in native_tools.TOOL_SPECS for feature in spec["feature_ids"]}, {"CM-018", "CM-050", "CM-052", "CM-062"})

    def test_missing_helper_does_not_produce_a_scan(self):
        with patch.object(native_tools, "_handshake", side_effect=CleanMacError("Build helper", "native_helper_missing")):
            result = self.dispatch("photos", "inspect")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["coverage"], "unscanned")
        self.assertEqual(self.store.list("scans"), [])

    def test_photos_denial_never_requests_access_implicitly(self):
        def call(command, payload, **kwargs):
            if command == "info": return self.info
            self.assertEqual(command, "photos-inspect")
            return {"status": "permission-needed", "authorization": "denied", "items": [], "coverage": "unscanned"}
        with patch.object(native_tools, "_call", side_effect=call) as native:
            result = self.dispatch("photos", "inspect")
        self.assertEqual(result["status"], "permission-needed")
        self.assertEqual(native.call_count, 2)

    def test_permission_request_requires_user_intent(self):
        with patch.object(native_tools, "_call") as native:
            with self.assertRaises(CleanMacError): self.dispatch("photos", "request-access")
        native.assert_not_called()

    def test_explicit_photos_permission_request(self):
        with patch.object(native_tools, "_handshake", return_value=self.info), patch.object(native_tools, "_call", return_value={"status": "permission-needed", "authorization": "denied"}) as native:
            result = self.dispatch("photos", "request-access", configuration={"request_permission": True})
        self.assertEqual(native.call_args.args[0], "photos-authorize")
        self.assertEqual(result["authorization"], "denied")

    def test_photos_rejects_filesystem_roots(self):
        with patch.object(native_tools, "_handshake", return_value=self.info), patch.object(native_tools, "_call") as native:
            with self.assertRaises(CleanMacError): self.dispatch("photos", "inspect", roots=[str(self.root)])
        native.assert_not_called()

    def test_protected_photos_cannot_be_planned(self):
        record = self.photo_record(protected=True)
        with self.assertRaises(CleanMacError):
            self.dispatch("photos", "plan", record_id=record["id"], selected=["photo-1"], configuration={"confirm_synced_deletion": True})

    def test_photo_deletion_needs_effect_review(self):
        record = self.photo_record()
        with self.assertRaises(CleanMacError): self.dispatch("photos", "plan", record_id=record["id"], selected=["photo-1"])

    def test_stale_photo_stops_before_mutation(self):
        record = self.photo_record()
        plan = self.dispatch("photos", "plan", record_id=record["id"], selected=["photo-1"], configuration={"confirm_synced_deletion": True})
        def call(command, payload, **kwargs):
            if command == "info": return self.info
            self.assertEqual(command, "photos-inspect")
            return {"status": "complete", "authorization": "authorized", "items": [{"id": "photo-1", "protected": False, "modified_at": 2}]}
        with patch.object(native_tools, "_call", side_effect=call):
            with self.assertRaises(CleanMacError):
                self.dispatch("photos", "apply", record_id=plan["id"], approve=plan["digest"])
        receipt = self.store.list("receipts")[0]
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["execution_started"])

    def test_exact_photo_delete_and_no_replay(self):
        record = self.photo_record()
        plan = self.dispatch("photos", "plan", record_id=record["id"], selected=["photo-1"], configuration={"confirm_synced_deletion": True})
        effects = []
        def call(command, payload, **kwargs):
            if command == "info": return self.info
            if command == "photos-inspect":
                self.assertEqual(payload["selected"], ["photo-1"])
                return record
            self.assertEqual(command, "photos-delete")
            effects.append(payload)
            return {"status": "complete", "deleted_asset_ids": ["photo-1"]}
        with patch.object(native_tools, "_call", side_effect=call):
            receipt = self.dispatch("photos", "apply", record_id=plan["id"], approve=plan["digest"])
            replay = self.dispatch("photos", "apply", record_id=plan["id"], approve=plan["digest"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt, replay)
        self.assertEqual(len(effects), 1)

    def test_mutation_timeout_requires_recovery(self):
        record = self.photo_record()
        plan = self.dispatch("photos", "plan", record_id=record["id"], selected=["photo-1"], configuration={"confirm_synced_deletion": True})
        def call(command, payload, **kwargs):
            if command == "info": return self.info
            if command == "photos-inspect": return record
            raise CleanMacError("timeout", "native_timeout")
        with patch.object(native_tools, "_call", side_effect=call):
            receipt = self.dispatch("photos", "apply", record_id=plan["id"], approve=plan["digest"])
        self.assertEqual(receipt["status"], "recovery_needed")

    def test_screen_duration_bounds(self):
        for duration in (0, 121, True, 1.5):
            with self.subTest(duration=duration), patch.object(native_tools, "_call") as native:
                with self.assertRaises(CleanMacError): self.dispatch("screen-cleaning", "plan", configuration={"duration": duration})
                native.assert_not_called()

    def test_screen_preflight_denial_stops_plan(self):
        with patch.object(native_tools, "_handshake", return_value=self.info), patch.object(native_tools, "_call", return_value={"status": "complete", "accessibility": False, "secure_input": False}):
            with self.assertRaises(CleanMacError): self.dispatch("screen-cleaning", "plan")
        self.assertEqual(self.store.list("plans"), [])

    def test_screen_exact_approval(self):
        with patch.object(native_tools, "_handshake", return_value=self.info), patch.object(native_tools, "_call", return_value={"status": "complete", "accessibility": True, "secure_input": False}):
            plan = self.dispatch("screen-cleaning", "plan", configuration={"duration": 10})
        with patch.object(native_tools, "_call") as native:
            with self.assertRaises(CleanMacError): self.dispatch("screen-cleaning", "apply", record_id=plan["id"], approve="0" * 64)
            native.assert_not_called()

    def test_document_requires_one_regular_file(self):
        with patch.object(native_tools, "_handshake", return_value=self.info):
            with self.assertRaises(CleanMacError): self.dispatch("document-versions", "inspect", roots=[str(self.root)])

    def test_document_symlink_is_not_followed(self):
        source = self.root / "document.txt"
        source.write_text("preserve")
        link = self.root / "alias.txt"
        link.symlink_to(source)
        with patch.object(native_tools, "_handshake", return_value=self.info), patch.object(native_tools, "_call") as native:
            with self.assertRaises(CleanMacError): self.dispatch("document-versions", "inspect", roots=[str(link)])
        native.assert_not_called()

    def test_document_respects_persistent_ignores(self):
        document = self.root / "ignored.txt"
        document.write_bytes(b"ignored fixture")
        self.store.save_settings({"ignore_paths": [str(document)]})
        with patch.object(native_tools, "_handshake", return_value=self.info), patch.object(native_tools, "_call") as native:
            with self.assertRaises(CleanMacError): self.dispatch("document-versions", "inspect", roots=[str(document)])
        native.assert_not_called()

    def test_document_protected_versions_cannot_be_planned(self):
        record = native_tools._scan_record(self.store, "document-versions", {"versions": [{"id": "current", "protected": True}]})
        with self.assertRaises(CleanMacError):
            self.dispatch("document-versions", "plan", record_id=record["id"], selected=["current"], configuration={"confirm_history_loss": True})

    def test_inspection_cancellation_has_no_native_effect(self):
        with patch.object(native_tools, "_call") as native:
            result = native_tools.dispatch("photos", "inspect", {}, store=self.store, cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        native.assert_not_called()


HELPER = Path(__file__).resolve().parents[1] / "native/bin/CleanMac Features.app/Contents/MacOS/cleanmac-features"


@unittest.skipUnless(sys.platform == "darwin" and HELPER.is_file(), "Build optional native features helper to exercise generated fixtures")
class NativeFeatureFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, command, payload):
        completed = subprocess.run([str(HELPER), command], input=json.dumps(payload), text=True, capture_output=True, timeout=30)
        return json.loads(completed.stdout)

    def bmp(self, path, width, height, color):
        stride = (width * 3 + 3) & ~3
        pixels = (bytes(color) * width + b"\0" * (stride - width * 3)) * height
        header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
        header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
        path.write_bytes(header + pixels)

    def png(self, path, color, alpha=False):
        def chunk(name, content):
            return struct.pack(">I", len(content)) + name + content + struct.pack(">I", zlib.crc32(name + content) & 0xffffffff)
        header = struct.pack(">IIBBBBB", 16, 16, 8, 6 if alpha else 2, 0, 0, 0)
        pixels = (b"\0" + bytes(color) * 16) * 16
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))

    def test_native_handshake_does_not_request_permissions(self):
        result = self.call("info", {})
        self.assertEqual(result["protocol_version"], 1)
        self.assertFalse(result["privileged"])

    def test_generated_resized_images_are_review_only(self):
        first, second = self.root / "red.bmp", self.root / "red-large.bmp"
        self.bmp(first, 20, 20, (0, 0, 255))
        self.bmp(second, 40, 40, (0, 0, 255))
        result = self.call("images-inspect", {"paths": [str(first), str(second)], "threshold": 8})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(result["pairs"]), 1)
        self.assertFalse(result["pairs"][0]["selected"])
        self.assertTrue(result["pairs"][0]["requires_visual_review"])
        self.assertEqual(result["algorithm"], native_tools.ALGORITHM)

    def test_malformed_image_and_symlink_are_not_decoded(self):
        source = self.root / "broken.png"
        source.write_bytes(b"not an image")
        alias = self.root / "alias.png"
        alias.symlink_to(source)
        result = self.call("images-inspect", {"paths": [str(source), str(alias)]})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["items"], [])
        self.assertEqual(len(result["errors"]), 2)

    def test_symlink_ancestor_is_not_followed(self):
        folder = self.root / "real"
        folder.mkdir()
        path = folder / "image.bmp"
        self.bmp(path, 4, 4, (255, 0, 0))
        alias = self.root / "linked"
        alias.symlink_to(folder, target_is_directory=True)
        result = self.call("images-inspect", {"paths": [str(alias / "image.bmp")]})
        self.assertEqual(result["items"], [])

    def test_alpha_distinctions_are_preserved(self):
        opaque, alpha = self.root / "opaque.png", self.root / "alpha.png"
        self.png(opaque, (255, 0, 0))
        self.png(alpha, (255, 0, 0, 128), alpha=True)
        result = self.call("images-inspect", {"paths": [str(opaque), str(alpha)]})
        self.assertEqual(result["status"], "complete")
        self.assertEqual({item["has_alpha"] for item in result["items"]}, {True, False})
        self.assertEqual(result["pairs"], [])

    def test_oversized_input_is_rejected_before_decode(self):
        image = self.root / "oversized.png"
        with image.open("wb") as output:
            output.truncate(64 * 1024 * 1024 + 1)
        result = self.call("images-inspect", {"paths": [str(image)]})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["errors"][0]["reason"], "unsupported_file")

    def test_document_inspection_preserves_generated_bytes(self):
        document = self.root / "document.txt"
        document.write_bytes(b"generated document fixture\n")
        before = document.read_bytes()
        result = self.call("versions-inspect", {"path": str(document)})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(document.read_bytes(), before)
        self.assertEqual(result["coverage"], "local-versions-only")

    def test_native_mutations_require_explicit_flags(self):
        for command in ("photos-delete", "photos-authorize", "screen-start", "apps-act"):
            with self.subTest(command=command):
                result = self.call(command, {})
                self.assertEqual(result["error"]["code"], "confirmation_required")
        for command, key in (("photos-authorize", "request_permission"), ("screen-start", "approved")):
            result = self.call(command, {key: 1})
            self.assertEqual(result["error"]["code"], "confirmation_required")

    def test_application_validation_rejects_incomplete_identity(self):
        result = self.call("apps-validate", {"action": "quit", "expected": {"pid": os.getpid()}})
        self.assertEqual(result["error"]["code"], "invalid_application_identity")

    def test_application_inspection_only_uses_selected_processes(self):
        result = self.call("apps-inspect", {"selected_pids": [os.getpid()]})
        returned = {item["identity"]["pid"] for item in result["items"]}
        self.assertTrue(returned.issubset({os.getpid()}))
        self.assertTrue(all(error["pid"] == os.getpid() for error in result["errors"]))

    def test_watchdog_rejects_python_parent_without_signalling_it(self):
        # Use a sacrificial Python caller too: a future regression must not
        # signal the test runner. The long deadline is cancelled by run's
        # timeout if the helper ever fails to reject before waiting.
        caller = '''import json, subprocess, sys
result = subprocess.run([sys.argv[1], "watchdog", "120"], capture_output=True, text=True, timeout=3)
print(json.dumps({"caller_alive": True, "returncode": result.returncode, "result": json.loads(result.stdout)}))
'''
        completed = subprocess.run([sys.executable, "-c", caller, str(HELPER)], capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["caller_alive"])
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["result"]["error"]["code"], "watchdog_parent_rejected")

    def test_watchdog_parent_preflight_does_not_lock_input(self):
        result = self.call("watchdog-preflight", {})
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue(result["eligible"])
        self.assertFalse(result["input_changed"])

    def test_watchdog_duration_is_bounded(self):
        for duration in ("0", "121", "-1", "invalid"):
            with self.subTest(duration=duration):
                completed = subprocess.run([str(HELPER), "watchdog", duration], capture_output=True, text=True, timeout=3)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(json.loads(completed.stdout)["error"]["code"], "invalid_arguments")

    def test_generated_version_selection_preserves_current_and_earliest(self):
        document = self.root / "versioned.txt"
        fixture = r'''import Foundation
let document = URL(fileURLWithPath: CommandLine.arguments[1])
let source = document.deletingLastPathComponent().appendingPathComponent("revision-fixture.txt")
try Data("current fixture bytes".utf8).write(to: document)
let coordinator = NSFileCoordinator()
var coordinateError: NSError?
var actionError: Error?
coordinator.coordinate(writingItemAt: document, options: .forMerging, error: &coordinateError) { item in
    do {
        for index in 0..<3 {
            try Data("revision \(index)".utf8).write(to: source)
            _ = try NSFileVersion.addOfItem(at: item, withContentsOf: source, options: [])
            Thread.sleep(forTimeInterval: 0.02)
        }
    } catch { actionError = error }
}
if let error = coordinateError { throw error }
if let error = actionError { throw error }
'''
        cleanup = r'''import Foundation
let document = URL(fileURLWithPath: CommandLine.arguments[1])
let coordinator = NSFileCoordinator()
var error: NSError?
coordinator.coordinate(writingItemAt: document, options: .forMerging, error: &error) { item in
    for version in NSFileVersion.otherVersionsOfItem(at: item) ?? [] { try? version.remove() }
}
'''
        try:
            setup = subprocess.run(["xcrun", "swift", "-e", fixture, str(document)], capture_output=True, text=True, timeout=60)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            first = self.call("versions-inspect", {"path": str(document)})
            self.assertEqual(first["status"], "complete")
            repeated = self.call("versions-inspect", {"path": str(document)})
            self.assertEqual(first["versions"], repeated["versions"], "persistent IDs must survive helper processes")
            earliest = [item for item in first["versions"] if "earliest" in item["protection_reasons"]]
            self.assertEqual(len(earliest), 1)
            self.assertFalse(earliest[0]["is_current"])
            eligible = [item for item in first["versions"] if not item["protected"]]
            self.assertGreaterEqual(len(eligible), 1)
            selected = eligible[-1]
            payload = {**first, "selected": [selected["id"]], "versions": [selected], "confirm_history_loss": True}
            applied = self.call("versions-remove", payload)
            self.assertEqual(applied["status"], "complete")
            self.assertEqual(applied["removed"], [selected["id"]])
            self.assertEqual(document.read_bytes(), b"current fixture bytes")
            missing = self.call("versions-remove", payload)
            self.assertEqual(missing["status"], "complete")
            self.assertEqual(missing["skipped_missing"], [selected["id"]])
        finally:
            if document.exists():
                subprocess.run(["xcrun", "swift", "-e", cleanup, str(document)], capture_output=True, timeout=60)

    @unittest.skipUnless(os.environ.get("CLEANMAC_APP_PROCESS_INTEGRATION") == "1", "Opt in to launch and close a disposable signed fixture app")
    def test_generated_app_quit_relaunch_and_quit(self):
        app = self.root / "ProcessFixture.app"
        binary = app / "Contents/MacOS/process-fixture"
        binary.parent.mkdir(parents=True)
        identifier = "org.example.processfixture." + uuid.uuid4().hex
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleName": "Disposable Process Fixture", "CFBundleExecutable": "process-fixture",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1"}))
        source = self.root / "process-fixture.swift"
        source.write_text('import AppKit\nimport Foundation\nlet application = NSApplication.shared\napplication.setActivationPolicy(.regular)\nlet output = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("fixture.pid")\ntry String(ProcessInfo.processInfo.processIdentifier).write(to:output,atomically:true,encoding:.utf8)\napplication.run()\n')
        subprocess.run(["xcrun", "swiftc", str(source), "-o", str(binary)], capture_output=True, check=True, timeout=60)
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=True, timeout=20)
        cleanup = 'import AppKit\nimport Foundation\nlet identifier = CommandLine.arguments[1]\nlet path = CommandLine.arguments[2]\nfor application in NSRunningApplication.runningApplications(withBundleIdentifier: identifier) { if application.bundleURL?.path == path { _ = application.forceTerminate() } }\n'
        try:
            subprocess.run(["/usr/bin/open", "-n", "-g", str(app)], capture_output=True, check=True, timeout=15)
            pid_path = self.root / "fixture.pid"
            for _ in range(50):
                if pid_path.exists(): break
                time.sleep(0.1)
            self.assertTrue(pid_path.exists(), "The generated fixture did not launch.")
            pid = int(pid_path.read_text())
            inspected = {"items": []}
            for _ in range(30):
                inspected = self.call("apps-inspect", {"selected_pids": [pid]})
                if inspected.get("items"): break
                time.sleep(0.1)
            self.assertEqual(len(inspected.get("items", [])), 1, inspected)
            item = inspected["items"][0]
            self.assertFalse(item["protected"], item)
            identity = item["identity"]
            validation = self.call("apps-validate", {"action": "quit", "expected": identity})
            diagnostics = ""
            if not validation.get("eligible"):
                diagnostics = subprocess.run(["xcrun", "swift", "-e", 'import Foundation; let bundle = Bundle(url: URL(fileURLWithPath: CommandLine.arguments[1]))!; print(bundle.executableURL?.path ?? "nil")', str(app)], capture_output=True, text=True, timeout=30).stdout
            self.assertTrue(validation.get("eligible"), (validation, identity["executable_path"], diagnostics))
            quit_result = self.call("apps-act", {"action": "quit", "expected": identity, "approved": True})
            self.assertEqual(quit_result.get("status"), "completed", quit_result)
            relaunched = self.call("apps-act", {"action": "relaunch", "expected": identity, "approved": True})
            self.assertEqual(relaunched.get("status"), "completed", relaunched)
            self.assertNotEqual(relaunched["new_identity"]["pid"], identity["pid"])
            final = self.call("apps-act", {"action": "quit", "expected": relaunched["new_identity"], "approved": True})
            self.assertEqual(final.get("status"), "completed", final)
        finally:
            subprocess.run(["xcrun", "swift", "-e", cleanup, identifier, str(app)], capture_output=True, timeout=60)


if __name__ == "__main__":
    unittest.main()
