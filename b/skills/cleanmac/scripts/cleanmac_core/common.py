"""Small shared primitives; no filesystem operations or global configuration."""
from __future__ import annotations

import datetime
import hashlib
import json
import unicodedata
import uuid


class CleanMacError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details

    def as_dict(self):
        error = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return error

    def to_dict(self):
        return self.as_dict()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def display_path(path) -> str:
    """Escape display controls, bidi overrides, surrogates and Markdown syntax."""
    result = []
    for char in str(path):
        code = ord(char)
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            result.append(f"\\u{code:04x}" if code <= 0xffff else f"\\U{code:08x}")
        elif char in "\\`*_{}[]<>|":
            result.append("\\" + char)
        else:
            result.append(char)
    return "".join(result)
