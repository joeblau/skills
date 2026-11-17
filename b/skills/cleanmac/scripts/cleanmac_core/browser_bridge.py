"""Private local transport for the optional Chrome native-messaging companion.

No HTTP listener, general shell execution, or page JavaScript is exposed. The
extension initiates a connection only when the user presses Connect. Each native
host process serves one extension profile and one ephemeral connection nonce.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import sys
import time
import uuid

from .common import CleanMacError, canonical_json, digest, utc_now

HOST_NAME = "com.joeblau.cleanmac"
PROTOCOL = 1
MAX_MESSAGE = 1024 * 1024
_SOCKET = re.compile(r"c-[0-9a-f]{16}\.sock\Z")
_PROFILE = re.compile(r"[0-9a-f-]{36}\Z")
READ_COMMANDS = {"tabs-inspect", "privacy-inspect"}
MUTATIONS = {"tab-close", "tab-discard", "tab-resume", "tab-reopen", "privacy-clear"}


def bridge_directory(store, *, create=False) -> Path:
    from .permissions import _open_directory
    path = store.root / "browser-bridge"
    # Validate the store's pinned identity as well as the child directory.
    with store._root_fd() as root:
        if create:
            try:
                os.mkdir("browser-bridge", 0o700, dir_fd=root)
            except FileExistsError:
                pass
        fd = _open_directory(str(path))
        try:
            value = os.fstat(fd)
            if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077:
                raise CleanMacError("Browser bridge directory must be owner-only.", code="browser_bridge_unsafe")
        finally:
            os.close(fd)
    return path


def _encoded(value) -> bytes:
    data = canonical_json(value).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise CleanMacError("Browser bridge response exceeds the limit; choose fewer tabs.", code="browser_bridge_limit")
    return struct.pack("<I", len(data)) + data


def _read_exact(read, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = read(size - len(result))
        if not chunk:
            raise EOFError("Browser bridge disconnected")
        result.extend(chunk)
    return bytes(result)


def read_message(read) -> dict:
    length = struct.unpack("<I", _read_exact(read, 4))[0]
    if not 1 <= length <= MAX_MESSAGE:
        raise CleanMacError("Invalid browser bridge message length.", code="browser_bridge_protocol")
    try:
        result = json.loads(_read_exact(read, length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise CleanMacError("Invalid browser bridge JSON.", code="browser_bridge_protocol") from error
    if not isinstance(result, dict):
        raise CleanMacError("Browser bridge messages must be objects.", code="browser_bridge_protocol")
    return result


def call_bridge(store, connection: str, command: str, arguments=None, *, approval=None, timeout=45) -> dict:
    if not isinstance(connection, str) or not _SOCKET.fullmatch(connection):
        raise CleanMacError("Select a connected Chrome profile from browser-bridge inspect.", code="browser_connection_required")
    if command not in READ_COMMANDS | MUTATIONS | {"describe"}:
        raise CleanMacError("Unknown browser bridge command.", code="browser_bridge_protocol")
    try:
        directory = bridge_directory(store)
        path = directory / connection
        metadata = path.lstat()
        if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise CleanMacError("Browser connection is not a private owner socket.", code="browser_bridge_unsafe")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(_encoded({"protocol_version": PROTOCOL, "command": command,
                                    "arguments": arguments or {}, "approval": approval}))
            response = read_message(client.recv)
    except (OSError, EOFError) as error:
        raise CleanMacError("The Chrome profile is disconnected or did not respond. Reconnect in the companion; inspect any pending receipt before retrying a mutation.",
                            code="browser_bridge_unavailable") from error
    if response.get("ok") is not True:
        failure = response.get("error", {})
        code = failure.get("code") if isinstance(failure, dict) else None
        allowed = {"browser_target_changed", "browser_action_unavailable", "browser_private_scope",
                   "browser_state_decision_required", "browser_bridge_protocol", "browser_approval_required"}
        raise CleanMacError("The browser declined the request. Refresh the profile and selected targets before preparing another action.",
                            code=code if code in allowed else "browser_bridge_error")
    result = response.get("result")
    if not isinstance(result, dict):
        raise CleanMacError("Browser response has an invalid shape.", code="browser_bridge_protocol")
    return result


def connections(store) -> list[dict]:
    try:
        directory = bridge_directory(store)
    except FileNotFoundError:
        return []
    except CleanMacError as error:
        if error.code == "state_not_found":
            return []
        raise
    result = []
    for path in sorted(directory.iterdir())[:32]:
        if not _SOCKET.fullmatch(path.name):
            continue
        try:
            result.append(dict(call_bridge(store, path.name, "describe", timeout=1), connection=path.name))
        except CleanMacError:
            continue  # Stale/disconnected sockets never become ready profiles.
    return result


def validate_request(store, request: dict, profile: dict) -> tuple[str, dict]:
    if (request.get("protocol_version") != PROTOCOL or not isinstance(request.get("arguments"), dict)
            or set(request) - {"protocol_version", "command", "arguments", "approval"}):
        raise CleanMacError("Invalid bridge request.", code="browser_bridge_protocol")
    command, arguments = request.get("command"), request["arguments"]
    if not isinstance(command, str) or command not in READ_COMMANDS | MUTATIONS | {"describe"}:
        raise CleanMacError("Command is not supported.", code="browser_bridge_protocol")
    if command in MUTATIONS:
        from .actions import validate_action_plan, validate_action_receipt
        proof = request.get("approval")
        if not isinstance(proof, dict) or set(proof) != {"plan_id", "digest"}:
            raise CleanMacError("A concrete approved plan is required.", code="browser_approval_required")
        tool = "browser-privacy" if command == "privacy-clear" else "browser-tabs"
        plan = validate_action_plan(store.get("plans", proof["plan_id"]), tool=tool, action=command)
        if (not isinstance(proof["digest"], str) or not hmac.compare_digest(plan["digest"], proof["digest"])
                or plan["payload"] != {"command": command, "arguments": arguments,
                                      "connection": profile["connection"]}
                or arguments.get("profile_id") != profile["profile_id"]
                or arguments.get("session_id") != profile["session_id"]):
            raise CleanMacError("Bridge command does not match its approved plan.", code="browser_approval_required")
        # Only an executor with a durable running receipt may dispatch mutations.
        receipt_id = "receipt-action-" + plan["id"].removeprefix("plan-action-")
        receipt = validate_action_receipt(store.get("receipts", receipt_id), plan)
        if (receipt.get("status") != "running" or receipt.get("execution_started") is not True
                or receipt.get("plan_digest") != plan["digest"] or receipt.get("plan_snapshot") != plan):
            raise CleanMacError("A running journaled action is required.", code="browser_approval_required")
    return command, arguments


def reserve_dispatch(store, request, profile):
    """At most one native dispatch per approved plan, including after crashes.

    The outer action executor journals the product outcome; this independent
    receipt prevents a surviving/restarted host from replaying a command while
    that outer receipt still says running. Reservation is never erased on error.
    """
    proof = request["approval"]
    identifier = "receipt-browser-" + proof["plan_id"].removeprefix("plan-action-")
    with store.lock("bridge-" + identifier):
        try:
            store.get("receipts", identifier)
        except CleanMacError as error:
            if error.code != "state_not_found": raise
        except FileNotFoundError:
            pass
        else:
            raise CleanMacError("This browser action was already dispatched; inspect its outcome before recovery.", code="browser_action_already_dispatched")
        receipt = {"schema_version": 1, "id": identifier, "record_type": "browser_dispatch", "created_at": utc_now(),
                   "plan_id": proof["plan_id"], "plan_digest": proof["digest"], "session_id": profile["session_id"],
                   "request_digest": digest(request), "status": "running", "restore_handle": {"kind": "browser_dispatch_audit"}}
        store.put("receipts", receipt)
        return receipt


def serve_native(store, extension_id: str, origin: str, *, source=None, destination=None):
    """Chrome launches this process; stdout is exclusively framed native messages."""
    if (not re.fullmatch(r"[a-p]{32}", extension_id)
            or origin != f"chrome-extension://{extension_id}/"):
        raise CleanMacError("Native host origin did not match its installation.", code="browser_bridge_protocol")
    source = source or sys.stdin.buffer
    destination = destination or sys.stdout.buffer
    hello = read_message(source.read)
    if (hello.get("type") != "hello" or hello.get("protocol_version") != PROTOCOL
            or not isinstance(hello.get("profile_id"), str) or not _PROFILE.fullmatch(hello["profile_id"])
            or hello.get("extension_id") != extension_id):
        raise CleanMacError("Invalid native bridge handshake.", code="browser_bridge_protocol")
    label = hello.get("label", "Chrome profile")
    if not isinstance(label, str) or not 1 <= len(label) <= 80 or any(ord(c) < 32 for c in label):
        raise CleanMacError("Invalid profile label.", code="browser_bridge_protocol")
    directory = bridge_directory(store, create=True)
    connection = f"c-{uuid.uuid4().hex[:16]}.sock"
    path = directory / connection
    if len(os.fsencode(path)) > 100:
        raise CleanMacError("Choose a shorter private CleanMac data directory for the browser socket.", code="browser_bridge_path_length")
    profile = {"connection": connection, "profile_id": hello["profile_id"], "label": label,
               "session_id": uuid.uuid4().hex, "browser": "chrome", "protocol_version": PROTOCOL,
               "private_browsing": "excluded"}

    def send(message):
        destination.write(_encoded(message))
        destination.flush()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(4)
        send({"type": "connected", "profile": profile})
        while True:
            ready, _, _ = select.select([server, source], [], [], 60)
            if source in ready:
                # A disconnected extension ends this host. Unsolicited commands
                # from the extension never execute operations on the computer.
                message = read_message(source.read)
                if message.get("type") == "disconnect":
                    break
                continue
            if server not in ready:
                continue
            client, _ = server.accept()
            with client:
                client.settimeout(45)
                try:
                    request = read_message(client.recv)
                    command, arguments = validate_request(store, request, profile)
                    if command == "describe":
                        response = {"ok": True, "result": profile}
                    else:
                        reservation = reserve_dispatch(store, request, profile) if command in MUTATIONS else None
                        identifier = uuid.uuid4().hex
                        send({"id": identifier, "command": command, "arguments": arguments,
                              "profile_id": profile["profile_id"], "session_id": profile["session_id"]})
                        if not select.select([source], [], [], 40)[0]:
                            raise CleanMacError("Browser action timed out; its effect is unknown.", code="browser_bridge_timeout")
                        response = read_message(source.read)
                        if response.get("id") != identifier:
                            raise CleanMacError("Browser reply was out of sequence.", code="browser_bridge_protocol")
                        if reservation:
                            reservation["status"] = "completed" if response.get("ok") is True else "recovery_needed"
                            reservation["updated_at"] = utc_now()
                            store.put("receipts", reservation)
                    client.sendall(_encoded(response))
                except (CleanMacError, OSError, EOFError) as error:
                    code = error.code if isinstance(error, CleanMacError) else "browser_bridge_unavailable"
                    try:
                        client.sendall(_encoded({"ok": False, "error": {"code": code}}))
                    except OSError:
                        pass
    except EOFError:
        pass
    finally:
        server.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
