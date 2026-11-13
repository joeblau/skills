"""Behavioral inventory checks; all filesystem fixtures are disposable."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cleanmac_core import inventory
from cleanmac_core.common import CleanMacError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanmac-inventory-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, relative, data=b"fixture"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, *roots, **kwargs):
        return inventory.scan_roots([str(root) for root in roots] or [str(self.root)], **kwargs)

    def item_for(self, scan, path):
        return next(value for value in scan["items"] if value["path"] == str(path))

    def test_metadata_only_hidden_files_and_exact_paths(self):
        self.write(".hidden", b"hidden")
        unusual = self.write("folder/name\nwith\ttabs", b"contents")
        original_open = os.open
        calls = []

        def tracked_open(path, flags, *args, **kwargs):
            calls.append((path, flags))
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(inventory.os, "open", side_effect=tracked_open):
            result = self.scan()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["files"], 2)
        self.assertEqual(result["counts"]["logical_bytes"], 14)
        self.assertTrue(all(flags & os.O_DIRECTORY for _, flags in calls))
        self.assertEqual(self.item_for(result, unusual)["path"], str(unusual))
        self.assertEqual(unusual.read_bytes(), b"contents")

    def test_ancestor_chain_and_stable_item_ids(self):
        file = self.write("nested/file")
        first = self.item_for(self.scan(), file)
        second = self.item_for(self.scan(), file)
        self.assertEqual(first["id"], second["id"])
        expected = [str(value) for value in reversed(file.parents)]
        self.assertEqual([a["path"] for a in first["ancestors"]], expected)
        self.assertTrue(inventory.validate_item_identity(first))
        fd = inventory.safe_open_item(first)
        try:
            self.assertEqual(os.read(fd, 100), b"fixture")
        finally:
            os.close(fd)

    def test_overlap_and_hard_links_have_honest_totals(self):
        file = self.write("nested/a", b"1234567")
        os.link(file, self.root / "nested" / "b")
        result = self.scan(self.root, self.root / "nested", self.root)
        self.assertEqual(result["counts"]["files"], 2)
        self.assertEqual(result["counts"]["unique_files"], 1)
        self.assertEqual(result["counts"]["duplicate_file_aliases"], 1)
        self.assertEqual(result["counts"]["logical_bytes"], 7)
        self.assertEqual(len({i["path"] for i in result["items"]}), len(result["items"]))
        self.assertIn("not-uniquely-reclaimable", result["accounting"]["allocated_bytes"])

    def test_symlink_and_symlink_ancestor_never_expand_scope(self):
        self.write("outside/secret")
        selected = self.root / "selected"
        selected.mkdir()
        (selected / "linked").symlink_to(self.root / "outside", target_is_directory=True)
        (selected / "file-link").symlink_to(self.root / "outside" / "secret")
        result = self.scan(selected)
        self.assertEqual(result["counts"]["symlinks"], 2)
        self.assertEqual(result["counts"]["files"], 0)
        self.assertEqual(self.scan(selected / "linked")["status"], "unavailable")
        self.assertEqual(self.scan(selected / "linked" / "secret")["status"], "unavailable")

    def test_packages_repositories_cloud_and_agent_data_are_not_traversed(self):
        for path in ("Example.app/Contents/private", "repo/.git/config", "repo/source.py",
                     "Library/CloudStorage/Provider/local", "Library/Mobile Documents/cloud",
                     ".codex/sessions/history", ".ssh/id_rsa", "Library/Mail/private",
                     "Library/Application Support/Claude/history", "backups.backupdb/archive"):
            self.write(path)
        result = self.scan()
        paths = {i["path"] for i in result["items"]}
        self.assertNotIn(str(self.root / "Example.app/Contents/private"), paths)
        self.assertNotIn(str(self.root / "repo/source.py"), paths)
        self.assertNotIn(str(self.root / ".codex/sessions/history"), paths)
        self.assertEqual(result["counts"]["files"], 0)
        self.assertGreater(result["counts"]["protected"], 5)

    def test_explicit_repository_subfolder_stays_protected(self):
        self.write("repo/.git/config")
        self.write("repo/sub/file")
        result = self.scan(self.root / "repo/sub")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"][0]["protection_reason"], "source-repository")

    def test_unknown_application_data_is_classified_for_rule_gating(self):
        paths = [self.write("Library/Application Support/Vendor/database"),
                 self.write("Library/Containers/vendor/Data/Library/Caches/cache"),
                 self.write(".npm/_cacache/content-v2/blob"), self.write(".config/vendor/settings")]
        result = self.scan()
        for path in paths:
            item = self.item_for(result, path)
            self.assertTrue(item["is_app_data"])
            self.assertEqual(item["category_hint"], "app-data")
        self.assertFalse(self.item_for(result, paths[0])["protected"])

    def test_placeholder_is_not_opened(self):
        path = self.write(".document.icloud", b"stub")
        item = self.item_for(self.scan(), path)
        self.assertTrue(item["is_placeholder"])
        self.assertTrue(item["protected"])
        with self.assertRaises(CleanMacError):
            inventory.safe_open_item(item)

    def test_changed_file_and_missing_chain_rejected(self):
        path = self.write("file")
        item = self.item_for(self.scan(), path)
        path.write_bytes(b"replacement contents")
        self.assertFalse(inventory.validate_item_identity(item))
        with self.assertRaises(CleanMacError):
            inventory.safe_open_item(item)
        changed = self.item_for(self.scan(), path)
        changed.pop("ancestors")
        self.assertFalse(inventory.validate_item_identity(changed))
        with self.assertRaises(CleanMacError):
            inventory.safe_open_item(changed)

    def test_parent_substitution_is_rejected(self):
        path = self.write("selected/file", b"original")
        self.write("outside/file", b"wrong")
        item = self.item_for(self.scan(self.root / "selected"), path)
        (self.root / "selected").rename(self.root / "old-selected")
        (self.root / "selected").symlink_to(self.root / "outside", target_is_directory=True)
        self.assertFalse(inventory.validate_item_identity(item))
        with self.assertRaises(CleanMacError):
            inventory.safe_open_item(item)

    def test_entry_limit_counts_excluded_entries(self):
        for index in range(20):
            (self.root / f"link-{index}").symlink_to("missing")
        result = self.scan(max_items=5)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["entries_seen"], 5)
        self.assertTrue(any(error["code"] == "item_limit" for error in result["errors"]))

    def test_cancellation_returns_partial_inventory(self):
        for index in range(20):
            self.write(f"file-{index}")
        count = 0

        def cancel():
            nonlocal count
            count += 1
            return count > 6

        result = self.scan(cancel=cancel)
        self.assertEqual(result["status"], "cancelled")
        self.assertLess(result["counts"]["files"], 20)

    def test_denial_is_coverage_error_not_empty_success(self):
        denied = self.root / "denied"
        denied.mkdir()
        denied_inode = denied.stat().st_ino
        original_scandir = os.scandir

        def controlled_scandir(fd):
            if os.fstat(fd).st_ino == denied_inode:
                raise PermissionError(errno.EACCES, "fixture denied")
            return original_scandir(fd)

        with mock.patch.object(inventory.os, "scandir", side_effect=controlled_scandir):
            result = self.scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][0]["code"], "access_denied")
        self.assertEqual(self.item_for(result, denied)["protection_reason"], "unreadable-directory")

    def test_exclusion_uses_path_boundaries(self):
        self.write("skip/secret")
        retained = self.write("skipper/visible")
        result = self.scan(exclude_paths=[self.root / "skip"])
        self.assertEqual(result["counts"]["files"], 1)
        self.assertEqual(self.item_for(result, retained)["path"], str(retained))
        self.assertEqual(self.scan(exclude_paths=[self.root])["status"], "unavailable")

    def test_unselected_volume_is_not_traversed(self):
        self.write("external/private")
        original_scandir = os.scandir

        class EntryProxy:
            def __init__(self, entry):
                self.entry = entry
                self.name = entry.name

            def stat(self, *, follow_symlinks):
                value = self.entry.stat(follow_symlinks=follow_symlinks)
                if self.name != "external":
                    return value
                fields = {name: getattr(value, name) for name in (
                    "st_ino", "st_mode", "st_size", "st_uid", "st_gid",
                    "st_nlink", "st_mtime_ns", "st_ctime_ns", "st_blocks",
                )}
                return SimpleNamespace(**fields, st_dev=value.st_dev + 1, st_flags=getattr(value, "st_flags", 0))

        class IteratorProxy:
            def __init__(self, fd):
                self.iterator = original_scandir(fd)

            def __next__(self):
                return EntryProxy(next(self.iterator))

            def close(self):
                self.iterator.close()

        with mock.patch.object(inventory.os, "scandir", side_effect=IteratorProxy):
            result = self.scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["files"], 0)
        external = self.item_for(result, self.root / "external")
        self.assertEqual(external["protection_reason"], "unselected-volume")

    def test_depth_limit_and_special_files(self):
        self.write("one/two/three/deep")
        os.mkfifo(self.root / "fifo")
        with mock.patch.object(inventory, "MAX_DEPTH", 2):
            result = self.scan()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["special_files"], 1)
        self.assertNotIn(str(self.root / "one/two/three/deep"), {item["path"] for item in result["items"]})

    def test_invalid_inputs_are_rejected(self):
        for limit in (0, -1, True, 1000001, "5"):
            with self.assertRaises(CleanMacError):
                self.scan(max_items=limit)
        with self.assertRaises(CleanMacError):
            inventory.scan_roots([])


if __name__ == "__main__":
    unittest.main()
