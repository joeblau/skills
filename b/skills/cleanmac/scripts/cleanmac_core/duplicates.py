"""Read-only exact-duplicate discovery using stable inventory identities.

Every content read uses inventory's descriptor-relative opener. A group is a
snapshot, not a deletion decision: plans must revalidate both targets and keepers.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import hashlib
import json
import os
import stat
from typing import Callable, Iterable

from .common import CleanMacError
from .inventory import assert_item_identity, safe_open_item

_CHUNK_SIZE = 1024 * 1024
_SAMPLE_SIZE = 64 * 1024
_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)


def _check_cancel(cancel: Callable | None) -> None:
    if cancel is not None and cancel():
        raise CleanMacError("Duplicate scan cancelled.", code="cancelled")


def _ineligible_reason(item: dict, min_size: int) -> str | None:
    if item.get("kind") != "file":
        return "not_regular_file"
    if item.get("protected"):
        return "protected"
    if item.get("is_placeholder") or int(item.get("flags", 0)) & _DATALESS:
        return "placeholder"
    if item.get("nlink") != 1:
        return "hard_link_or_unknown_link_count"
    size = item.get("logical_size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "invalid_size"
    if size < min_size:
        return "below_minimum_size"
    return None


def _check_stat(item: dict, current: os.stat_result) -> None:
    expected = (
        item.get("device"), item.get("inode"), item.get("logical_size"),
        item.get("mtime_ns"), item.get("ctime_ns"), item.get("mode"),
        item.get("nlink"), item.get("flags", 0),
    )
    actual = (
        current.st_dev, current.st_ino, current.st_size,
        current.st_mtime_ns, current.st_ctime_ns, current.st_mode,
        current.st_nlink, getattr(current, "st_flags", 0),
    )
    if not stat.S_ISREG(current.st_mode) or expected != actual:
        raise CleanMacError(
            "File changed since inventory; scan it again.",
            code="file_changed", details={"item_id": item.get("id")},
        )
    if getattr(current, "st_flags", 0) & _DATALESS:
        raise CleanMacError("Cloud-only content is excluded.", code="unsafe_item")


@contextmanager
def _verified_file(item: dict):
    reason = _ineligible_reason(item, 0)
    if reason is not None:
        raise CleanMacError(
            "Item is not eligible for duplicate content reads.",
            code="unsafe_item", details={"item_id": item.get("id"), "reason": reason},
        )
    fd = None
    try:
        fd = safe_open_item(item)
        _check_stat(item, os.fstat(fd))
        yield fd
        _check_stat(item, os.fstat(fd))
        # Check that the pathname and parents still name the opened snapshot.
        assert_item_identity(item)
    except OSError as error:
        raise CleanMacError(
            "Could not read an inventoried file.", code="read_failed",
            details={"item_id": item.get("id"), "errno": error.errno},
        ) from error
    finally:
        if fd is not None:
            os.close(fd)


def hash_item(item: dict, *, cancel: Callable | None = None) -> str:
    """Hash one eligible snapshot, rejecting changed paths, parents, or data.

    Use this for duplicate target/keeper revalidation. File bytes are read in
    bounded chunks and never returned or persisted. Cancellation raises a
    CleanMacError whose code is ``cancelled``.
    """
    _check_cancel(cancel)
    hasher = hashlib.sha256()
    with _verified_file(item) as fd:
        while True:
            _check_cancel(cancel)
            chunk = os.read(fd, _CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _sample_hash(item: dict, cancel: Callable | None) -> str:
    """Cheap rejection stage; a sample match never establishes equality."""
    hasher = hashlib.sha256()
    size = item["logical_size"]
    with _verified_file(item) as fd:
        offsets = [0]
        if size > _SAMPLE_SIZE:
            offsets.append(max(_SAMPLE_SIZE, size - _SAMPLE_SIZE))
        for offset in offsets:
            _check_cancel(cancel)
            os.lseek(fd, offset, os.SEEK_SET)
            remaining = min(_SAMPLE_SIZE, max(0, size - offset))
            hasher.update(offset.to_bytes(8, "big"))
            while remaining:
                _check_cancel(cancel)
                chunk = os.read(fd, remaining)
                if not chunk:
                    raise CleanMacError("File became shorter during sampling.", code="file_changed")
                hasher.update(chunk)
                remaining -= len(chunk)
    return hasher.hexdigest()


def _equal_content(left: dict, right: dict, cancel: Callable | None) -> bool:
    """Confirm hash matches with byte comparison before calling them exact."""
    equal = True
    with _verified_file(left) as left_fd, _verified_file(right) as right_fd:
        while True:
            _check_cancel(cancel)
            left_chunk = os.read(left_fd, _CHUNK_SIZE)
            right_chunk = os.read(right_fd, _CHUNK_SIZE)
            if left_chunk != right_chunk:
                equal = False
                break
            if not left_chunk:
                break
    return equal


def _stable_group(items: list[dict], sha256: str) -> dict:
    ordered = sorted(items, key=lambda item: (os.fsencode(item["path"]), item["id"]))
    item_ids = [item["id"] for item in ordered]
    identity = json.dumps([sha256, item_ids], separators=(",", ":"), ensure_ascii=True)
    return {
        "id": "duplicate-" + hashlib.sha256(identity.encode("ascii")).hexdigest()[:24],
        "item_ids": item_ids,
        "keeper_id": item_ids[0],
        "sha256": sha256,
        "logical_size": ordered[0]["logical_size"],
        "extra_copy_logical_size": ordered[0]["logical_size"] * (len(ordered) - 1),
        "keeper_is_suggestion": True,
    }


def find_duplicates(
    items: Iterable[dict], *, cancel: Callable | None = None,
    progress: Callable | None = None, min_size: int = 1,
) -> dict:
    """Find verified equal files without selecting any file for removal.

    File reads are bounded to a fixed chunk size. Candidate metadata uses O(n)
    memory within the inventory's item bound. Protected/nonlocal/hard-linked
    items are intentionally skipped; read failures make the result partial.
    """
    if not isinstance(min_size, int) or isinstance(min_size, bool) or min_size < 0:
        raise CleanMacError("Minimum duplicate size must be a nonnegative integer.")
    counts = {"input": 0, "eligible": 0, "sampled": 0, "hashed": 0, "compared": 0}
    skips: dict[str, int] = defaultdict(int)
    errors: list[dict] = []
    groups: list[dict] = []
    by_size: dict[int, list[dict]] = defaultdict(list)
    seen_identities: set[tuple] = set()
    seen_ids: set[str] = set()
    current_item: dict | None = None
    cancelled = False

    def report(phase: str) -> None:
        if progress is not None:
            progress({"phase": "duplicates_" + phase, **counts, "groups": len(groups)})

    def record_error(item: dict, error: CleanMacError) -> None:
        errors.append({
            "item_id": item.get("id"), "path": item.get("path"),
            "code": error.code, "message": str(error),
        })

    try:
        report("grouping")
        for current_item in items:
            _check_cancel(cancel)
            counts["input"] += 1
            reason = _ineligible_reason(current_item, min_size)
            if reason is not None:
                skips[reason] += 1
                continue
            item_id = current_item.get("id")
            identity = (current_item.get("device"), current_item.get("inode"))
            if not isinstance(item_id, str) or not item_id or any(x is None for x in identity):
                record_error(current_item, CleanMacError("Missing stable file identity.", code="invalid_item"))
                continue
            if item_id in seen_ids or identity in seen_identities:
                skips["repeated_identity"] += 1
                continue
            seen_ids.add(item_id)
            seen_identities.add(identity)
            by_size[current_item["logical_size"]].append(current_item)
            counts["eligible"] += 1

        for size in sorted(by_size):
            candidates = by_size[size]
            if len(candidates) < 2:
                continue
            sampled: dict[str, list[dict]] = defaultdict(list)
            for current_item in candidates:
                _check_cancel(cancel)
                try:
                    sample = _sample_hash(current_item, cancel)
                except CleanMacError as error:
                    if error.code == "cancelled":
                        raise
                    record_error(current_item, error)
                    continue
                sampled[sample].append(current_item)
                counts["sampled"] += 1
                report("sampling")
            for same_sample in sampled.values():
                if len(same_sample) < 2:
                    continue
                hashed: dict[str, list[dict]] = defaultdict(list)
                for current_item in same_sample:
                    _check_cancel(cancel)
                    try:
                        sha256 = hash_item(current_item, cancel=cancel)
                    except CleanMacError as error:
                        if error.code == "cancelled":
                            raise
                        record_error(current_item, error)
                        continue
                    hashed[sha256].append(current_item)
                    counts["hashed"] += 1
                    report("hashing")
                for sha256, matches in hashed.items():
                    if len(matches) < 2:
                        continue
                    # Partition even a hypothetical hash collision into actual
                    # byte-equal groups. No hashes alone authorize deletion.
                    equal_sets: list[list[dict]] = []
                    for current_item in matches:
                        _check_cancel(cancel)
                        try:
                            placed = False
                            for equal_set in equal_sets:
                                if _equal_content(equal_set[0], current_item, cancel):
                                    equal_set.append(current_item)
                                    placed = True
                                    break
                                counts["compared"] += 1
                            if not placed:
                                assert_item_identity(current_item)
                                equal_sets.append([current_item])
                            else:
                                counts["compared"] += 1
                            report("comparing")
                        except CleanMacError as error:
                            if error.code == "cancelled":
                                raise
                            record_error(current_item, error)
                    for equal_set in equal_sets:
                        if len(equal_set) < 2:
                            continue
                        stable_items = []
                        for current_item in equal_set:
                            _check_cancel(cancel)
                            try:
                                assert_item_identity(current_item)
                            except CleanMacError as error:
                                record_error(current_item, error)
                                continue
                            stable_items.append(current_item)
                        if len(stable_items) >= 2:
                            groups.append(_stable_group(stable_items, sha256))
    except CleanMacError as error:
        if error.code != "cancelled":
            raise
        cancelled = True

    groups.sort(key=lambda group: (-group["extra_copy_logical_size"], group["id"]))
    counts["duplicate_groups"] = len(groups)
    counts["duplicate_files"] = sum(len(group["item_ids"]) for group in groups)
    counts["skipped"] = sum(skips.values())
    result = {
        "groups": groups, "errors": errors,
        "status": "cancelled" if cancelled else "partial" if errors else "complete",
        "counts": counts, "skipped_by_reason": dict(skips),
        "min_size": min_size, "selection": [],
    }
    report("cancelled" if cancelled else "finished")
    return result
