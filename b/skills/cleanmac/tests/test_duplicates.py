"""Behavioral fixtures for exact duplicate discovery; never use personal files."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanmac_core.common import CleanMacError
from cleanmac_core import duplicates
from cleanmac_core.inventory import scan_roots


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="cleanmac-duplicate-fixture-")
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, data=b"identical fixture bytes"):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def items(self):
        return scan_roots([str(self.root)])["items"]

    def file_items(self):
        return [item for item in self.items() if item["kind"] == "file"]

    def test_exact_duplicates_have_stable_suggested_keeper_and_no_selection(self):
        self.write("zeta.bin")
        self.write("alpha.bin")
        self.write("different.bin", b"different fixture bytes")
        items = self.items()
        result = duplicates.find_duplicates(items)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["selection"], [])
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        by_id = {item["id"]: item for item in items}
        self.assertEqual(Path(by_id[group["keeper_id"]]["path"]).name, "alpha.bin")
        self.assertTrue(group["keeper_is_suggestion"])
        self.assertEqual(group["sha256"], hashlib.sha256(b"identical fixture bytes").hexdigest())
        self.assertEqual(len(group["item_ids"]), 2)
        reversed_result = duplicates.find_duplicates(reversed(items))
        self.assertEqual(result["groups"], reversed_result["groups"])

    def test_sample_collision_does_not_make_different_files_duplicates(self):
        edge = b"a" * (64 * 1024)
        self.write("first.bin", edge + b"b" * len(edge) + edge)
        self.write("second.bin", edge + b"c" * len(edge) + edge)
        result = duplicates.find_duplicates(self.items())
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["counts"]["hashed"], 2)

    def test_content_comparison_rejects_a_hash_collision(self):
        self.write("first.bin", b"aaa")
        self.write("second.bin", b"bbb")
        with patch.object(duplicates, "_sample_hash", return_value="collision"), patch.object(
            duplicates, "hash_item", return_value="0" * 64
        ):
            result = duplicates.find_duplicates(self.items())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["groups"], [])

    def test_hard_link_aliases_are_not_extra_copies(self):
        first = self.write("first.bin")
        os.link(first, self.root / "linked.bin")
        self.write("independent.bin")
        result = duplicates.find_duplicates(self.items())
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["skipped_by_reason"]["hard_link_or_unknown_link_count"], 2)

    def test_repeated_inventory_identity_is_not_a_duplicate(self):
        self.write("only.bin")
        item = self.file_items()[0]
        result = duplicates.find_duplicates([item, dict(item)])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["skipped_by_reason"]["repeated_identity"], 1)

    def test_protected_and_cloud_only_items_are_never_opened(self):
        self.write("first.bin")
        self.write("second.bin")
        self.write("third.bin")
        items = self.file_items()
        items[0]["protected"] = True
        items[1]["is_placeholder"] = True
        items[2]["flags"] = items[2].get("flags", 0) | duplicates._DATALESS
        with patch.object(duplicates, "safe_open_item", side_effect=AssertionError("must not open")):
            result = duplicates.find_duplicates(items)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["counts"]["skipped"], 3)

    def test_changed_file_is_partial_and_not_a_duplicate(self):
        first = self.write("first.bin")
        self.write("second.bin")
        items = self.items()
        first.write_bytes(b"changed content now!!!")
        result = duplicates.find_duplicates(items)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["groups"], [])
        self.assertTrue(result["errors"])

    def test_symlink_replacement_is_not_followed(self):
        first = self.write("first.bin")
        other = self.write("second.bin")
        items = self.items()
        first.unlink()
        first.symlink_to(other)
        result = duplicates.find_duplicates(items)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["groups"], [])

    def test_ancestor_replacement_is_not_followed(self):
        self.write("nested/first.bin")
        self.write("second.bin")
        items = self.items()
        (self.root / "nested").rename(self.root / "relocated")
        (self.root / "nested").symlink_to(self.root / "relocated", target_is_directory=True)
        result = duplicates.find_duplicates(items)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["groups"], [])

    def test_read_denial_is_partial_without_claiming_clean(self):
        self.write("first.bin")
        self.write("second.bin")
        with patch.object(duplicates, "safe_open_item", side_effect=PermissionError(errno.EACCES, "denied")):
            result = duplicates.find_duplicates(self.items())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(all(error["code"] == "read_failed" for error in result["errors"]))

    def test_changed_during_read_is_not_reported_as_equal(self):
        first = self.write("first.bin", b"a" * 200000)
        self.write("second.bin", b"a" * 200000)
        items = self.items()
        real_read = os.read
        changed = False

        def mutate_after_read(fd, amount):
            nonlocal changed
            data = real_read(fd, amount)
            if not changed:
                changed = True
                first.write_bytes(b"b" * 200000)
            return data

        with patch.object(duplicates.os, "read", side_effect=mutate_after_read):
            result = duplicates.find_duplicates(items)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["groups"], [])

    def test_hash_item_is_cancellable_between_bounded_reads(self):
        self.write("first.bin", b"a" * 20000)
        item = self.file_items()[0]
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 4

        with patch.object(duplicates, "_CHUNK_SIZE", 4096):
            with self.assertRaises(CleanMacError) as raised:
                duplicates.hash_item(item, cancel=cancel)
        self.assertEqual(raised.exception.code, "cancelled")

    def test_cancelled_scan_has_explicit_status(self):
        self.write("first.bin")
        self.write("second.bin")
        result = duplicates.find_duplicates(self.items(), cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["groups"], [])

    def test_progress_is_actual_and_scan_makes_no_writes(self):
        self.write("first.bin")
        self.write("second.bin")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        progress = []
        result = duplicates.find_duplicates(self.items(), progress=progress.append)
        after = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(progress[-1]["phase"], "duplicates_finished")
        self.assertEqual(progress[-1]["hashed"], result["counts"]["hashed"])
        self.assertEqual(result["counts"]["hashed"], 2)

    def test_unicode_and_newline_paths_are_kept_as_data(self):
        self.write("a\n\u2603.bin")
        self.write("b.bin")
        result = duplicates.find_duplicates(self.items())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["groups"]), 1)

    def test_explicit_zero_size_threshold_supports_empty_files(self):
        self.write("first.bin", b"")
        self.write("second.bin", b"")
        self.assertEqual(duplicates.find_duplicates(self.items())["groups"], [])
        result = duplicates.find_duplicates(self.items(), min_size=0)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["logical_size"], 0)

    def test_invalid_min_size_is_rejected(self):
        for value in [-1, True, 0.5, "1"]:
            with self.subTest(value=value), self.assertRaises(CleanMacError):
                duplicates.find_duplicates([], min_size=value)


if __name__ == "__main__":
    unittest.main()
