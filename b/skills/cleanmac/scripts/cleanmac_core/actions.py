"""Approval-bound, journaled actions for provider and system adapters.

This module does not run commands or infer whether an action is safe. An adapter
supplies a read-only ``validate(plan)`` callback that checks the current account,
target, ownership, revision and other preconditions, and an ``execute(plan)``
callback that performs exactly the approved mutation. Each receives a fresh copy
of the saved plan. The validator must raise on any mismatch; None, True, or a
JSON object means that validation succeeded. An executor returns a JSON object
only after its effect is verified, and must raise for partial or unknown results.

One plan describes one journaled operation. Multi-step adapters must journal their
individual effects themselves or use separate plans. The running receipt is
durable before execution starts. Exceptions after that point are ambiguous and
never cause an automatic repeat, including a timeout or a cancellation. A normal
return records completion; replay returns that same receipt without callbacks.

Digests detect changes and bind approval to concrete contents; they are not user
consent or authentication against a process with the same user's state access.
Never persist credentials in payloads, preconditions, summaries, or results.
"""

from __future__ import annotations

import hmac
import json
import math
import re
from contextvars import ContextVar
from typing import Any, Callable

from .common import CleanMacError, canonical_json, digest, new_id, utc_now


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PLAN_ID = re.compile(r"plan-action-([0-9a-f]{32})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 4 * 1024 * 1024
_MAX_PLAN_BYTES = 1024 * 1024
_MAX_NODES = 100000
_PLAN_KEYS = frozenset({
    "schema_version", "record_type", "id", "created_at", "tool", "action",
    "payload", "summary", "preconditions", "status", "digest",
})
_RECEIPT_KEYS = frozenset({
    "schema_version", "record_type", "id", "created_at", "updated_at", "plan_id",
    "plan_digest", "plan_snapshot", "tool", "action", "status", "phase",
    "execution_started", "attempts", "result", "error", "restore_handle", "checkpoint",
    "integrity_digest",
})
_STATUSES = frozenset({"not_started", "running", "completed", "failed", "recovery_needed"})
_ACTIVE_ACTION = ContextVar("cleanmac_active_action", default=None)


class ActionNotStarted(CleanMacError):
    """Only for a proven no-effect failure inside execute, before any mutation.

    Do not use for a provider timeout, cancellation after dispatch, a partially
    applied batch, an unsuccessful response whose effects are unclear, or failed
    post-mutation verification. Such failures must remain recovery_needed.
    """

    def __init__(self, message="The action was not started.", code="action_not_started", details=None):
        super().__init__(message, code=code, details=details)


def _fail(message: str, code: str = "invalid_action_plan", **details) -> None:
    raise CleanMacError(message, code=code, details=details or None)


def _json_copy(value: Any) -> Any:
    """Reject Python-only coercions, deeply nested input and unbounded records."""
    nodes = 0

    def inspect(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES or depth > 32:
            _fail("The action record exceeds the supported complexity.")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                inspect(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                inspect(child, depth + 1)
            return
        _fail("Action records must contain ordinary finite JSON values.")

    inspect(value, 0)
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise CleanMacError("The action record cannot be represented as JSON.", code="invalid_action_plan") from error
    if len(encoded.encode("ascii")) > _MAX_BYTES:
        _fail("The action record exceeds the supported size.")
    return json.loads(encoded)


def _name(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        _fail(f"The action {label} is invalid.")


def _plan_digest(plan: dict) -> str:
    return digest({key: value for key, value in plan.items() if key != "digest"})


def _check_plan(plan: dict, *, tool: str, action: str) -> dict:
    _name(tool, "tool")
    _name(action, "name")
    value = _json_copy(plan)
    if not isinstance(value, dict) or set(value) != _PLAN_KEYS:
        _fail("The saved record is not a supported action plan.")
    if len(canonical_json(value).encode("ascii")) > _MAX_PLAN_BYTES:
        _fail("The action plan exceeds the supported size.")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "action_plan" or value["status"] != "planned"
            or not isinstance(value["id"], str) or not _PLAN_ID.fullmatch(value["id"])
            or not isinstance(value["created_at"], str) or not value["created_at"]
            or not isinstance(value["payload"], dict) or not isinstance(value["preconditions"], dict)
            or type(value["summary"]) not in (str, dict) or not value["summary"]):
        _fail("The action plan has invalid fields.")
    if value["tool"] != tool or value["action"] != action:
        _fail("The plan belongs to another tool or action.", code="action_scope_mismatch")
    expected = _plan_digest(value)
    if (not isinstance(value["digest"], str) or not _HASH.fullmatch(value["digest"])
            or not hmac.compare_digest(expected, value["digest"])):
        _fail("The action plan changed after it was prepared.", code="action_plan_changed")
    return value


def validate_action_plan(plan: dict, *, tool: str, action: str) -> dict:
    """Check plan schema, exact digest and dispatch scope, without approving it."""
    return _check_plan(plan, tool=tool, action=action)


def create_action_plan(store, tool: str, action: str, payload: dict,
                       summary: str | dict, preconditions: dict | None = None) -> dict:
    """Save an exact reviewable plan without performing or authorizing it."""
    _name(tool, "tool")
    _name(action, "name")
    if not isinstance(payload, dict) or (preconditions is not None and not isinstance(preconditions, dict)):
        _fail("Action payload and preconditions must be JSON objects.")
    if type(summary) not in (str, dict) or not summary:
        _fail("An action requires a nonempty review summary.")
    plan = _json_copy({
        "schema_version": 1, "record_type": "action_plan",
        "id": new_id("plan-action"), "created_at": utc_now(),
        "tool": tool, "action": action, "payload": payload, "summary": summary,
        "preconditions": {} if preconditions is None else preconditions,
        "status": "planned",
    })
    plan["digest"] = _plan_digest(plan)
    return store.put("plans", _check_plan(plan, tool=tool, action=action))


def _receipt_id(plan: dict) -> str:
    return "receipt-action-" + _PLAN_ID.fullmatch(plan["id"]).group(1)


def _new_receipt(plan: dict) -> dict:
    return {
        "schema_version": 1, "record_type": "action_receipt", "id": _receipt_id(plan),
        "created_at": utc_now(), "updated_at": utc_now(), "plan_id": plan["id"],
        "plan_digest": plan["digest"], "plan_snapshot": _json_copy(plan),
        "tool": plan["tool"], "action": plan["action"], "status": "not_started",
        "phase": "preflight", "execution_started": False, "attempts": 0,
        "result": None, "error": None, "restore_handle": None, "checkpoint": None,
    }


def _integrity(record: dict) -> str:
    return digest({key: value for key, value in record.items() if key != "integrity_digest"})


def _audit_handle(plan: dict) -> dict:
    # This is an audit-retention marker, not a claim of undo support. StateStore
    # protects restore_handle-bearing receipts from ordinary history deletion.
    return {"kind": "action_audit", "receipt_id": _receipt_id(plan),
            "plan_id": plan["id"], "reversible": "adapter-dependent"}


def _save_receipt(store, receipt: dict) -> dict:
    receipt["updated_at"] = utc_now()
    receipt["integrity_digest"] = _integrity(receipt)
    return store.put("receipts", receipt)


def _check_receipt(receipt: dict, plan: dict) -> dict:
    value = _json_copy(receipt)
    if not isinstance(value, dict) or set(value) != _RECEIPT_KEYS:
        _fail("The existing action receipt is invalid; preserve it for inspection.", code="invalid_action_receipt")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["record_type"] != "action_receipt" or value["id"] != _receipt_id(plan)
            or value["plan_id"] != plan["id"] or value["plan_digest"] != plan["digest"]
            or value["plan_snapshot"] != plan or value["tool"] != plan["tool"]
            or value["action"] != plan["action"] or type(value["status"]) is not str
            or value["status"] not in _STATUSES or type(value["phase"]) is not str
            or value["phase"] not in {"preflight", "execution", "verification"}
            or type(value["attempts"]) is not int or value["attempts"] < 0
            or type(value["execution_started"]) is not bool
            or type(value["created_at"]) is not str or not value["created_at"]
            or type(value["updated_at"]) is not str or not value["updated_at"]
            or (value["result"] is not None and type(value["result"]) is not dict)
            or (value["error"] is not None and type(value["error"]) is not dict)
            or (value["checkpoint"] is not None and type(value["checkpoint"]) is not dict)
            or not isinstance(value["integrity_digest"], str)
            or not _HASH.fullmatch(value["integrity_digest"])
            or not hmac.compare_digest(_integrity(value), value["integrity_digest"])):
        _fail("The action receipt does not match its approved plan.", code="invalid_action_receipt")
    started = value["execution_started"]
    if (started != (value["status"] in {"running", "completed", "recovery_needed"})
            or (started and (value["restore_handle"] != _audit_handle(plan) or value["attempts"] < 1))
            or (not started and value["restore_handle"] is not None)
            or (value["status"] == "completed" and value["result"] is None)):
        _fail("The receipt has inconsistent execution state.", code="invalid_action_receipt")
    return value


def validate_action_receipt(receipt: dict, plan: dict) -> dict:
    """Check an original receipt before deriving a separate restore plan.

    Adapters must additionally check status, their result/checkpoint schema,
    recovery eligibility and current remote identities. This is not an undo.
    """
    if not isinstance(plan, dict):
        _fail("The action receipt needs its original saved plan.")
    checked = _check_plan(plan, tool=plan.get("tool"), action=plan.get("action"))
    return _check_receipt(receipt, checked)


def checkpoint_action(data: dict) -> dict:
    """Durably save recovery metadata from inside the current execute callback.

    Record the intended next step before dispatching it, then update with the
    observed result. Data replaces the previous checkpoint, so multi-step
    adapters include their accumulated completed steps. This does not classify
    any operation as safe to repeat and does not replace final verification.
    """
    active = _ACTIVE_ACTION.get()
    if active is None:
        _fail("Checkpoints are only permitted inside an executing action.", code="invalid_action_checkpoint")
    if not isinstance(data, dict):
        _fail("An action checkpoint must be a JSON object.", code="invalid_action_checkpoint")
    value = _json_copy(data)
    if len(canonical_json(value).encode("ascii")) > 512 * 1024:
        _fail("The action checkpoint exceeds the supported size.", code="invalid_action_checkpoint")
    store, receipt = active
    if receipt["status"] != "running" or receipt["execution_started"] is not True:
        _fail("This action no longer accepts checkpoints.", code="invalid_action_checkpoint")
    receipt["checkpoint"] = value
    _save_receipt(store, receipt)
    return _json_copy(value)


def _error(error: BaseException, *, started: bool) -> dict:
    if isinstance(error, CleanMacError):
        # Do not copy arbitrary details or underlying provider errors into state.
        return {"code": error.code, "message": error.message}
    return {
        "code": "action_outcome_unknown" if started else "action_validation_failed",
        "message": ("The action stopped after starting. Inspect its effects before any further action."
                    if started else "The action could not validate its current preconditions. No action was started."),
        "exception_type": type(error).__name__,
    }


def _progress(callback, phase: str, plan: dict) -> None:
    if callback is not None:
        try:
            callback({"phase": phase, "tool": plan["tool"], "action": plan["action"]})
        except Exception:
            # A display failure cannot change whether a durable operation ran.
            pass


def execute_action_plan(store, plan_id: str, approve: str, tool: str, action: str,
                        validate: Callable[[dict], Any], execute: Callable[[dict], dict], *,
                        cancel: Callable[[], bool] | None = None, progress=None) -> dict:
    """Validate and execute once, returning the durable receipt.

    Approval/shape/validation failures raise CleanMacError. Cancellation before
    execution returns a not_started receipt. A callback exception after starting
    returns recovery_needed (KeyboardInterrupt/SystemExit are journaled then
    re-raised). ActionNotStarted records a safe-to-retry failed receipt. Replays of
    completed or ambiguous receipts never invoke validate or execute again.
    """
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        _fail("Choose a saved action plan ID.")
    if not callable(validate) or not callable(execute):
        _fail("An action requires explicit validation and execution callbacks.", code="invalid_action_adapter")
    if cancel is not None and not callable(cancel):
        _fail("Action cancellation must be a callable.", code="invalid_action_adapter")
    with store.lock("action-" + _PLAN_ID.fullmatch(plan_id).group(1)):
        plan = _check_plan(store.get("plans", plan_id), tool=tool, action=action)
        if not isinstance(approve, str) or not _HASH.fullmatch(approve) or not hmac.compare_digest(approve, plan["digest"]):
            _fail("Approve the exact reviewed action plan before applying it.", code="approval_required", plan_id=plan_id)
        try:
            receipt = _check_receipt(store.get("receipts", _receipt_id(plan)), plan)
        except CleanMacError as error:
            if error.code != "state_not_found":
                raise
            receipt = _new_receipt(plan)
            _save_receipt(store, receipt)
        if receipt["status"] in {"running", "recovery_needed"}:
            if receipt["status"] == "running":
                receipt.update(status="recovery_needed", error={
                    "code": "action_interrupted", "message": "Execution was interrupted. Inspect the action's actual effects; it was not repeated."})
                _save_receipt(store, receipt)
            return receipt
        if receipt["status"] == "completed":
            return receipt
        receipt.update(status="not_started", phase="preflight", error=None, result=None, checkpoint=None)
        _save_receipt(store, receipt)

        def cancelled() -> bool:
            if cancel is not None and cancel():
                receipt.update(status="not_started", error={"code": "action_cancelled", "message": "Cancelled before execution; no action was started."})
                _save_receipt(store, receipt)
                return True
            return False

        try:
            if cancelled():
                return receipt
            _progress(progress, "action_validating", plan)
            evidence = validate(_json_copy(plan))
            if evidence is False:
                _fail("The action's current preconditions do not match the plan.", code="action_precondition_failed")
            if evidence is not None and evidence is not True and not isinstance(evidence, dict):
                _fail("The action validator returned an unsupported result.", code="invalid_action_adapter")
            if evidence is not None:
                _json_copy(evidence)
            if cancelled():
                return receipt
        except BaseException as error:
            receipt.update(status="failed", error=_error(error, started=False))
            _save_receipt(store, receipt)
            if isinstance(error, (CleanMacError, KeyboardInterrupt, SystemExit)):
                raise
            raise CleanMacError("The current action preconditions could not be verified; no action was started.",
                                code="action_validation_failed", details={"receipt_id": receipt["id"]}) from error

        receipt.update(status="running", phase="execution", execution_started=True,
                       attempts=receipt["attempts"] + 1, restore_handle=_audit_handle(plan))
        _save_receipt(store, receipt)
        _progress(progress, "action_executing", plan)
        active_token = _ACTIVE_ACTION.set((store, receipt))
        try:
            if cancel is not None and cancel():
                raise ActionNotStarted("Cancelled immediately before execution; no action was started.", code="action_cancelled")
            result = execute(_json_copy(plan))
            if not isinstance(result, dict):
                _fail("The action did not return a verifiable result.", code="action_outcome_unknown")
            result = _json_copy(result)
            if len(canonical_json(result).encode("ascii")) > 1024 * 1024:
                _fail("The action result exceeds the supported receipt size.", code="action_outcome_unknown")
            if "status" in result and result["status"] not in {"completed", "success", "ok"}:
                _fail("The action returned an incomplete or unsuccessful result. Inspect its actual effects.", code="action_outcome_unknown")
            receipt.update(status="completed", phase="verification", result=result, error=None)
            _save_receipt(store, receipt)
        except ActionNotStarted as error:
            receipt.update(status="failed", phase="preflight", execution_started=False,
                           restore_handle=None, result=None, checkpoint=None, error=_error(error, started=False))
            _save_receipt(store, receipt)
        except BaseException as error:
            receipt.update(status="recovery_needed", phase="execution", result=None, error=_error(error, started=True))
            _save_receipt(store, receipt)
            if not isinstance(error, Exception):
                raise
        finally:
            _ACTIVE_ACTION.reset(active_token)
        _progress(progress, "action_finished", plan)
        return receipt
