from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import rules
from cleanmac_core.common import CleanMacError


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-rules-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir(mode=0o700)

    def file(self, relative, data=b"fixture data"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def item(self, path):
        info = path.lstat()
        ancestors = []
        for parent in reversed(path.parents):
            value = parent.lstat()
            ancestors.append({"path": str(parent), "device": value.st_dev, "inode": value.st_ino})
        return {
            "id": "item-" + hashlib.sha256(os.fsencode(path)).hexdigest()[:24],
            "path": str(path), "root": str(self.home), "kind": "file",
            "device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
            "logical_size": info.st_size, "allocated_size": info.st_blocks * 512,
            "nlink": info.st_nlink, "flags": getattr(info, "st_flags", 0),
            "protected": False, "is_placeholder": False, "ancestors": ancestors,
        }

    def npm(self, data=b"a cached package response"):
        hexdigest = hashlib.sha512(data).hexdigest()
        path = self.file(".npm/_cacache/content-v2/sha512/" + "/".join((hexdigest[:2], hexdigest[2:4], hexdigest[4:])), data)
        return path, self.item(path)

    def classify(self, item, active_apps=()):
        return rules.classify_items([item], home=self.home, active_apps=active_apps)[0]

    def test_catalog_has_provenance_and_only_one_conditional_handler(self):
        catalog = rules.load_rules()
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual([r["id"] for r in catalog["rules"] if r["mode"] == "conditional"], ["npm-cacache-sha512-v2"])
        for rule in catalog["rules"]:
            self.assertTrue(rule["provenance"])
            self.assertTrue(rule["exclusions"])
            self.assertTrue(rule["applicability"])

    def test_valid_cache_blob_is_conditional_and_no_files_written(self):
        path, item = self.npm()
        before = {(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in self.home.rglob("*")}
        finding = self.classify(item)
        self.assertTrue(finding["eligible"])
        self.assertEqual(finding["content_sha512"], hashlib.sha512(path.read_bytes()).hexdigest())
        self.assertEqual(finding["rule_id"], "npm-cacache-sha512-v2")
        self.assertEqual(finding["process_status"], "inactive")
        self.assertEqual(before, {(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in self.home.rglob("*")})

    def test_modified_content_in_digest_named_file_is_not_eligible(self):
        path, _ = self.npm()
        path.write_bytes(b"not the named content")
        result = self.classify(self.item(path))
        self.assertFalse(result["eligible"])
        self.assertIn("digest", result["reason"])

    def test_stale_inventory_is_rejected(self):
        path, item = self.npm()
        path.write_bytes(b"changed after scan")
        result = self.classify(item)
        self.assertFalse(result["eligible"])
        self.assertIn("identity", result["reason"])

    def test_symlink_file_is_not_opened(self):
        path, item = self.npm()
        destination = self.file("outside.txt")
        path.unlink()
        path.symlink_to(destination)
        result = self.classify(item)
        self.assertFalse(result["eligible"])
        self.assertEqual(destination.read_bytes(), b"fixture data")

    def test_replaced_fifo_is_rejected_without_waiting_for_writer(self):
        path, item = self.npm()
        path.unlink()
        os.mkfifo(path)
        self.assertFalse(self.classify(item)["eligible"])

    def test_symlink_ancestor_is_not_followed(self):
        path, item = self.npm()
        original = path.parent
        moved = original.with_name(original.name + "-saved")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        result = self.classify(item)
        self.assertFalse(result["eligible"])

    def test_replaced_regular_ancestor_identity_is_rejected(self):
        path, item = self.npm()
        original = path.parent
        old = original.with_name(original.name + "-saved")
        original.rename(old)
        original.mkdir()
        (old / path.name).rename(path)
        fresh = self.item(path)
        # Preserve current file metadata, while giving the original ancestor identity.
        fresh["ancestors"] = item["ancestors"]
        self.assertFalse(self.classify(fresh)["eligible"])

    def test_npm_index_and_unknown_algorithm_are_report_only(self):
        for relative in (".npm/_cacache/index-v5/aa/bb", ".npm/_cacache/content-v2/sha1/aa/bb/1234", ".npm/_cacache/tmp/download"):
            with self.subTest(relative=relative):
                self.assertFalse(self.classify(self.item(self.file(relative)))["eligible"])

    def test_node_and_package_manager_processes_block_cleanup(self):
        _, item = self.npm()
        for executable in ("node", "/opt/homebrew/bin/node", "npm", "npm exec some-tool", "pnpm", "bun"):
            with self.subTest(executable=executable):
                result = self.classify(item, active_apps=[executable])
                self.assertFalse(result["eligible"])
                self.assertEqual(result["process_status"], "active")

    def test_unknown_process_state_is_not_empty_process_state(self):
        _, item = self.npm()
        with patch.object(rules, "detect_active_apps", return_value=None):
            result = rules.classify_items([item], home=self.home)[0]
        self.assertFalse(result["eligible"])
        self.assertEqual(result["process_status"], "unknown")

    def test_default_classification_obtains_fresh_process_state(self):
        _, item = self.npm()
        with patch.object(rules, "detect_active_apps", return_value=[]) as detect:
            self.assertTrue(rules.classify_items([item], home=self.home)[0]["eligible"])
        detect.assert_called_once()

    def test_invalid_process_snapshot_is_unknown(self):
        _, item = self.npm()
        self.assertFalse(self.classify(item, active_apps="") ["eligible"])

    def test_protected_placeholder_and_hardlinked_items_cannot_be_overridden(self):
        path, item = self.npm()
        for flag, value in (("protected", True), ("is_placeholder", True), ("kind", "directory"), ("nlink", 2)):
            with self.subTest(flag=flag):
                changed = dict(item, **{flag: value})
                self.assertFalse(self.classify(changed)["eligible"])
        link = self.home / "linked-blob"
        os.link(path, link)
        self.assertFalse(self.classify(self.item(path))["eligible"])

    def test_hash_limits_are_report_only_not_partial_validation(self):
        _, item = self.npm()
        with patch.object(rules, "_MAX_FILE_HASH_BYTES", 1):
            self.assertFalse(self.classify(item)["eligible"])
        with patch.object(rules, "_MAX_TOTAL_HASH_BYTES", 1):
            self.assertFalse(self.classify(item)["eligible"])

    def test_total_hash_budget_is_shared_across_items(self):
        _, first = self.npm(b"first")
        _, second = self.npm(b"second")
        with patch.object(rules, "_MAX_TOTAL_HASH_BYTES", 5):
            findings = rules.classify_items([first, second], home=self.home, active_apps=[])
        self.assertEqual([f["eligible"] for f in findings], [True, False])

    def test_cancelled_classification_does_not_query_processes(self):
        _, item = self.npm()
        with patch.object(rules, "detect_active_apps") as detect:
            result = rules.classify_items([item], home=self.home, cancel=lambda: True)
        self.assertEqual(result, [])
        detect.assert_not_called()

    def test_cancel_during_hash_does_not_publish_incomplete_finding(self):
        path, item = self.npm(b"x" * (2 * 1024 * 1024))
        read = rules.os.read
        cancelled = False
        read_bytes = 0

        def interrupt_read(fd, size):
            nonlocal cancelled, read_bytes
            block = read(fd, size)
            read_bytes += len(block)
            cancelled = True
            return block

        with patch.object(rules.os, "read", side_effect=interrupt_read):
            findings = rules.classify_items([item], home=self.home, active_apps=[], cancel=lambda: cancelled)
        self.assertEqual(findings, [])
        self.assertLess(read_bytes, path.stat().st_size)

    def test_file_outside_home_does_not_gain_npm_rule(self):
        other_home = self.home.with_name("other")
        other_home.mkdir()
        _, item = self.npm()
        result = rules.classify_items([item], home=other_home, active_apps=[])[0]
        self.assertFalse(result["eligible"])
        self.assertEqual(result["rule_id"], "unknown-app-data-review")

    def test_prefix_collision_does_not_gain_rule(self):
        path = self.file("Library/CachesOther/file")
        self.assertEqual(rules.classify_items([self.item(path)], home=self.home, active_apps=[]), [])

    def test_generic_cache_and_unknown_app_data_stay_report_only(self):
        for relative in ("Library/Caches/unknown/blobs", "Library/Application Support/Unknown/store", ".cache/new-tool/data"):
            with self.subTest(relative=relative):
                finding = self.classify(self.item(self.file(relative)))
                self.assertFalse(finding["eligible"])
                self.assertEqual(finding["rule_id"], "unknown-app-data-review")
        item = self.item(self.file("custom-app-data/data"))
        item["is_app_data"] = True
        self.assertFalse(self.classify(item)["eligible"])

    def test_user_documents_omitted_and_metadata_not_mutated(self):
        item = self.item(self.file("Downloads/report.pdf"))
        original = json.loads(json.dumps(item))
        self.assertEqual(rules.classify_items([item], home=self.home, active_apps=[]), [])
        self.assertEqual(item, original)

    def test_media_and_tool_candidates_preserve_content(self):
        cases = {
            "Library/Application Support/Spotify/PersistentCache/Storage/offline": "spotify-cache-review",
            "Library/Application Support/Adobe/Common/Media Cache Files/media": "media-app-cache-review",
            "Library/Application Support/Telegram Desktop/tdata/session": "media-app-cache-review",
            "Library/Containers/com.tencent.xinWeChat/Data/messages": "media-app-cache-review",
            "Library/Application Support/UnknownElectron/Cache/cache": "electron-cache-review",
            ".gradle/caches/modules-2/package.jar": "gradle-cache-review",
            "Library/Developer/Xcode/DerivedData/ModuleCache.noindex/hash/module.pcm": "xcode-module-cache-review",
            "Library/Caches/pip/http-v2/body": "pip-cache-review",
            "Organization Dropbox/.dropbox.cache/upload": "dropbox-cache-review",
            "Library/Group Containers/TEAM.com.getdropbox.dropbox.sync/root-mount/upload": "dropbox-cache-review",
        }
        for relative, expected in cases.items():
            with self.subTest(relative=relative):
                path = self.file(relative)
                result = self.classify(self.item(path))
                self.assertEqual(result["rule_id"], expected)
                self.assertFalse(result["eligible"])
                self.assertEqual(path.read_bytes(), b"fixture data")

    def test_licenses_credentials_and_agent_sessions_remain_protected(self):
        for relative in ("Library/Caches/unknown/license.key", "Library/Caches/unknown/auth.json", "Library/Application Support/Code/Cache/.codex/session", "Library/Caches/unknown/.env"):
            with self.subTest(relative=relative):
                result = self.classify(self.item(self.file(relative)))
                self.assertFalse(result["eligible"])
                self.assertIn("protected", result["reason"])

    def test_default_roots_are_existing_narrow_and_not_symlinks(self):
        cache = self.home / "Library/Caches"
        cache.mkdir(parents=True)
        npm = self.home / ".npm/_cacache"
        npm.mkdir(parents=True)
        gradle = self.home / ".gradle/caches"
        gradle.parent.mkdir()
        gradle.symlink_to(cache, target_is_directory=True)
        self.assertEqual(rules.default_cache_roots(self.home), [str(cache), str(npm)])
        self.assertNotIn(str(self.home), rules.default_cache_roots(self.home))

    def test_invalid_conditional_catalog_revision_fails_closed(self):
        document = rules.load_rules()
        document["rules"][0]["version"] = "2.0.0"
        source = self.file("catalog.json", json.dumps(document).encode())
        with patch.object(rules, "_CATALOG", source):
            with self.assertRaises(CleanMacError):
                rules.load_rules()

    def test_missing_unknown_protection_and_wrong_catalog_type_fail_closed(self):
        for document in ([], {"schema_version": 1, "rules": []}):
            with self.subTest(document=document):
                source = self.file("catalog.json", json.dumps(document).encode())
                with patch.object(rules, "_CATALOG", source):
                    with self.assertRaises(CleanMacError):
                        rules.load_rules()

    def test_process_query_does_not_read_or_expose_command_arguments(self):
        completed = subprocess.CompletedProcess([], 0, "/usr/bin/node\n/usr/bin/python3\n", "")
        with patch.object(rules.subprocess, "run", return_value=completed) as run:
            self.assertEqual(rules.detect_active_apps(), ["node", "python3"])
        self.assertEqual(run.call_args.args[0], ["/bin/ps", "-axo", "comm="])
        failed = subprocess.CompletedProcess([], 1, "", "do not expose this")
        with patch.object(rules.subprocess, "run", return_value=failed):
            self.assertIsNone(rules.detect_active_apps())

    def test_invalid_home_and_missing_identity_do_not_enable_rule(self):
        _, item = self.npm()
        with self.assertRaises(CleanMacError):
            rules.classify_items([item], home="/", active_apps=[])
        item.pop("ancestors")
        self.assertFalse(self.classify(item)["eligible"])


if __name__ == "__main__":
    unittest.main()
