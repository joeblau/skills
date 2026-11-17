import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cleanmac_core import browser_tools as browser, browser_bridge as bridge
from cleanmac_core.actions import create_action_plan, execute_action_plan
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore

PROFILE = {"connection": "c-1234567890abcdef.sock", "profile_id": "12345678-1234-1234-1234-123456789abc",
           "session_id": "a" * 32, "label": "Personal", "browser": "chrome", "protocol_version": 1}


def tab(identifier=5, url="https://example.test/path", **changes):
    value = {"tab_id": identifier, "window_id": 9, "url": url, "url_digest": hashlib.sha256(url.encode()).hexdigest(),
             "identity": "abcdefab-1234-1234-1234-123456789abc", "revision": 0,
             "title": "Example", "active": False, "pinned": False, "audible": False, "discarded": False,
             "auto_discardable": True, "observed_at": time.time() * 1000 - 120000, "age_source": "first-observed", "incognito": False}
    value.update(changes)
    return value


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-browser-", dir="/tmp")
        self.root = Path(self.temp.name).resolve()
        self.store = StateStore(self.root / "state")
        self.tabs = [tab()]
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def fake_bridge(self, store, connection, command, arguments=None, **kwargs):
        self.requests.append((command, arguments, kwargs))
        if connection != PROFILE["connection"]: raise AssertionError(connection)
        if command == "describe": return dict(PROFILE)
        if command == "tabs-inspect": return dict(PROFILE, items=self.tabs, complete=True, total=len(self.tabs))
        if command == "privacy-inspect": return dict(PROFILE, supported_types=list(browser._CATEGORIES.values()))
        if command.startswith("tab-") or command == "privacy-clear": return {"status": "completed", "operation": command}
        raise AssertionError(command)

    def inspect(self, **config):
        return browser.dispatch("browser-tabs", "inspect", {"configuration": {"connection": PROFILE["connection"], **config}}, store=self.store)

    def plan(self, scan, **config):
        return browser.dispatch("browser-tabs", "plan", {"record_id": scan["id"], "selected": [scan["items"][0]["id"]],
            "configuration": {"operation": "close", "allow_form_loss": True, **config}}, store=self.store)

    def test_catalog_discovery_is_offline(self):
        self.assertEqual(len(browser.TOOL_SPECS), 5)
        with patch.object(bridge, "call_bridge", side_effect=AssertionError("network")):
            result = browser.dispatch("browser-privacy", "inspect", {"configuration": {}}, store=self.store)
        self.assertEqual(result["status"], "manual")
        password = next(row for row in result["categories"] if row["category"] == "passwords")
        self.assertEqual(password["automated_status"], "unavailable")

    def test_fresh_bridge_inspection_returns_setup_instructions(self):
        result = browser.dispatch("browser-bridge", "inspect", {}, store=self.store)
        self.assertEqual(result["status"], "setup-needed")
        self.assertEqual(result["profiles"], [])
        self.assertFalse((self.store.root / "browser-bridge").exists())

    def test_metadata_inventory_never_reads_or_changes_database(self):
        root = self.root / "profile"; root.mkdir()
        history = root / "History"; history.write_bytes(b"private database data")
        before = history.read_bytes()
        with patch("builtins.open", side_effect=AssertionError("content opened")):
            result = browser.dispatch("browser-privacy", "inspect", {"roots": [str(root)]}, store=self.store)
        self.assertTrue(result["profiles"][0]["stores"])
        self.assertEqual(history.read_bytes(), before)
        self.assertTrue(all(row["content_read"] is False for row in result["profiles"][0]["stores"]))

    def test_metadata_symlink_profile_is_denied(self):
        target = self.root / "real"; target.mkdir()
        link = self.root / "link"; link.symlink_to(target)
        result = browser.dispatch("browser-privacy", "inspect", {"roots": [str(link)]}, store=self.store)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["profiles"], [])

    def test_metadata_missing_root_reports_unscanned(self):
        result = browser.dispatch("browser-privacy", "inspect", {"roots": [str(self.root / "missing")]}, store=self.store)
        self.assertEqual(result["errors"][0]["coverage"], "unscanned")

    def test_recent_lists_do_not_read_documents(self):
        root = self.root / "recent"; root.mkdir()
        path = root / "com.example.recent.sfl3"; path.write_bytes(b"opaque private format")
        result = browser.dispatch("recent-items", "inspect", {"roots": [str(root)]}, store=self.store)
        self.assertEqual(result["status"], "manual")
        self.assertEqual(len(result["stores"]), 1)
        self.assertEqual(path.read_bytes(), b"opaque private format")

    def test_tab_urls_redacted_and_exact_grouping_preserves_query(self):
        self.tabs = [tab(1, "https://example.test/?auth=one#one"), tab(2, "https://example.test/?auth=two#one"), tab(3, "https://example.test/?auth=one#one")]
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            scan = self.inspect()
        self.assertEqual(len(scan["duplicate_groups"]), 1)
        self.assertEqual(scan["duplicate_groups"][0]["item_ids"], ["tab-1", "tab-3"])
        serialized = json.dumps(scan)
        self.assertNotIn("auth=", serialized)
        self.assertNotIn("#one", serialized)
        self.assertEqual(scan["items"][0]["age_source"], "unknown-before-observation")
        self.assertIsNone(scan["items"][0]["old"])

    def test_creation_observed_age_only(self):
        self.tabs = [tab(age_source="creation-observed")]
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge): scan = self.inspect(old_after_seconds=60)
        self.assertTrue(scan["items"][0]["old"])

    def test_incognito_response_rejected(self):
        self.tabs = [tab(incognito=True)]
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge), self.assertRaises(CleanMacError): self.inspect()

    def test_changed_profile_response_rejected(self):
        def call(store, connection, command, arguments=None, **kwargs):
            value = self.fake_bridge(store, connection, command, arguments, **kwargs)
            if command == "tabs-inspect": value["profile_id"] = "different"
            return value
        with patch.object(bridge, "call_bridge", side_effect=call), self.assertRaises(CleanMacError): self.inspect()

    def test_unknown_form_state_requires_decision(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            scan = self.inspect()
            with self.assertRaises(CleanMacError) as caught: self.plan(scan, allow_form_loss=False)
        self.assertEqual(caught.exception.code, "browser_state_decision_required")

    def test_pinned_active_audible_require_individual_decisions(self):
        for field in ("pinned", "active", "audible"):
            self.tabs = [tab(**{field: True})]
            with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
                scan = self.inspect()
                with self.assertRaises(CleanMacError): self.plan(scan)
                self.assertEqual(self.plan(scan, **{"allow_" + field: True})["action"], "tab-close")

    def test_discard_refuses_active_or_non_discardable(self):
        for values in ({"active": True}, {"auto_discardable": False}, {"discarded": True}):
            self.tabs = [tab(**values)]
            with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
                scan = self.inspect()
                with self.assertRaises(CleanMacError): self.plan(scan, operation="discard", allow_active=True)

    def test_resume_requires_discarded_tab(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            with self.assertRaises(CleanMacError): self.plan(self.inspect(), operation="resume")
            self.tabs = [tab(discarded=True)]
            plan = self.plan(self.inspect(), operation="resume")
        self.assertTrue(plan["summary"]["resume_activates_tab"])

    def test_single_target_per_receipt(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge): scan = self.inspect()
        with self.assertRaises(CleanMacError):
            browser.dispatch("browser-tabs", "plan", {"record_id": scan["id"], "selected": ["tab-5", "tab-6"], "configuration": {"operation": "close"}}, store=self.store)

    def test_navigation_after_review_prevents_mutation(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            plan = self.plan(self.inspect())
            self.tabs = [tab(url="https://example.test/new")]
            with self.assertRaises(CleanMacError):
                browser.dispatch("browser-tabs", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertFalse(any(command == "tab-close" for command, _, _ in self.requests))

    def test_navigation_back_to_same_url_invalidates_revision(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            plan = self.plan(self.inspect())
            self.tabs = [tab(revision=2)]
            with self.assertRaises(CleanMacError):
                browser.dispatch("browser-tabs", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertFalse(any(command == "tab-close" for command, _, _ in self.requests))

    def test_apply_idempotence_and_reopen_receipt(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            plan = self.plan(self.inspect(retain_reopen_url=True))
            options = {"record_id": plan["id"], "approve": plan["digest"]}
            receipt = browser.dispatch("browser-tabs", "apply", options, store=self.store)
            browser.dispatch("browser-tabs", "apply", options, store=self.store)
            reopen = browser.dispatch("browser-tabs", "plan", {"record_id": receipt["id"], "configuration": {"operation": "reopen"}}, store=self.store)
        self.assertEqual(sum(command == "tab-close" for command, _, _ in self.requests), 1)
        self.assertEqual(reopen["action"], "tab-reopen")
        self.assertEqual(reopen["payload"]["arguments"]["window_id"], 9)

    def test_sensitive_url_not_retained_for_reopen(self):
        self.tabs = [tab(url="https://example.test/login?token=secret")]
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            plan = self.plan(self.inspect(retain_reopen_url=True))
        self.assertNotIn("reopen_url", plan["payload"]["arguments"])
        self.assertNotIn("secret", json.dumps(plan))

    def test_close_wrong_approval_stops_before_bridge_mutation(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            plan = self.plan(self.inspect())
            with self.assertRaises(CleanMacError):
                browser.dispatch("browser-tabs", "apply", {"record_id": plan["id"], "approve": "0" * 64}, store=self.store)
        self.assertFalse(any(command == "tab-close" for command, _, _ in self.requests))

    def test_privacy_passwords_and_origin_filtered_history_rejected(self):
        for config in ({"category": "passwords", "since_ms": 0}, {"category": "history", "since_ms": 0, "origins": ["https://example.test"]}):
            with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge), self.assertRaises(CleanMacError):
                browser.dispatch("browser-privacy", "plan", {"configuration": {"connection": PROFILE["connection"], **config}}, store=self.store)

    def test_privacy_requires_explicit_time_range(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge), self.assertRaises(CleanMacError):
            browser.dispatch("browser-privacy", "plan", {"configuration": {"connection": PROFILE["connection"], "category": "cookies"}}, store=self.store)

    def test_privacy_origins_are_validated_before_action(self):
        for origin in ("https://example.test:443", "https://example.test/path", "https://EXAMPLE.test", "https://example.test:99999", "https://user:secret@example.test"):
            with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge), self.assertRaises(CleanMacError):
                browser.dispatch("browser-privacy", "plan", {"configuration": {"connection": PROFILE["connection"], "category": "cookies", "since_ms": 0, "origins": [origin]}}, store=self.store)
        self.assertEqual(browser._origin("https://example.test:8443"), "https://example.test:8443")

    def test_privacy_matrix_respects_browser_policy(self):
        def response(store, connection, command, arguments=None, **kwargs):
            value = self.fake_bridge(store, connection, command, arguments, **kwargs)
            if command == "privacy-inspect": value["removal_permitted"] = {"history": False}
            return value
        with patch.object(bridge, "call_bridge", side_effect=response):
            result = browser.dispatch("browser-privacy", "inspect", {"configuration": {"connection": PROFILE["connection"]}}, store=self.store)
        history = next(row for row in result["categories"] if row["category"] == "history")
        self.assertEqual(history["automated_status"], "unavailable")

    def test_download_history_action_scope_and_receipt(self):
        with patch.object(bridge, "call_bridge", side_effect=self.fake_bridge):
            plan = browser.dispatch("browser-privacy", "plan", {"configuration": {"connection": PROFILE["connection"], "category": "downloads", "since_ms": 0}}, store=self.store)
            receipt = browser.dispatch("browser-privacy", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
        self.assertEqual(plan["payload"]["arguments"]["types"], ["downloads"])
        self.assertEqual(receipt["status"], "completed")

    def test_bridge_setup_is_exact_and_does_not_overwrite(self):
        registration = self.root / "registration" / "host.json"
        config = {"extension_id": "a" * 32}
        with patch.object(browser, "_registration_path", return_value=registration), patch.object(browser, "_macos"):
            plan = browser.dispatch("browser-bridge", "plan", {"configuration": config}, store=self.store)
            self.assertFalse(registration.exists())
            receipt = browser.dispatch("browser-bridge", "apply", {"record_id": plan["id"], "approve": plan["digest"]}, store=self.store)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(json.loads(registration.read_text())["allowed_origins"], ["chrome-extension://" + "a" * 32 + "/"])
            with self.assertRaises(CleanMacError): browser.dispatch("browser-bridge", "plan", {"configuration": config}, store=self.store)

    def test_bridge_setup_symlink_parent_never_writes_target(self):
        target = self.root / "target"; target.mkdir()
        link = self.root / "alias"; link.symlink_to(target)
        with self.assertRaises(OSError): browser._create_private_file(link / "host.json", b"x", 0o600)
        self.assertFalse((target / "host.json").exists())

    def test_metrics_counts_pid_once_and_omits_other_apps(self):
        output = "1 0 2.5 100 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n1 0 2.5 100 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n2 0 5 200 /Applications/Other.app/Contents/MacOS/Other\n"
        with patch.object(browser, "_macos"), patch.object(browser.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            result = browser.dispatch("browser-resources", "inspect", {}, store=self.store)
        self.assertEqual(len(result["processes"]), 1)
        self.assertEqual(result["resident_bytes_sum"], 102400)
        self.assertEqual(result["per_tab"], "unavailable")

    def test_bridge_framing_rejects_excessive_or_nonobject_data(self):
        for data in (struct.pack("<I", bridge.MAX_MESSAGE + 1), struct.pack("<I", 2) + b"[]"):
            with self.assertRaises(CleanMacError): bridge.read_message(io.BytesIO(data).read)

    def test_bridge_socket_path_traversal_rejected(self):
        with self.assertRaises(CleanMacError): bridge.call_bridge(self.store, "../other.sock", "describe")

    def test_host_requires_approval_and_running_receipt(self):
        request = {"protocol_version": 1, "command": "tab-close", "arguments": {}, "approval": None}
        with self.assertRaises(CleanMacError): bridge.validate_request(self.store, request, PROFILE)

    def test_host_matches_plan_and_prevents_duplicate_dispatch(self):
        arguments = {"profile_id": PROFILE["profile_id"], "session_id": PROFILE["session_id"], "tab": {"tab_id": 5}}
        payload = {"connection": PROFILE["connection"], "command": "tab-close", "arguments": arguments}
        plan = create_action_plan(self.store, "browser-tabs", "tab-close", payload, "Close test tab")
        request = {"protocol_version": 1, "command": "tab-close", "arguments": arguments, "approval": {"plan_id": plan["id"], "digest": plan["digest"]}}
        with self.assertRaises(CleanMacError): bridge.validate_request(self.store, request, PROFILE)
        def perform(current):
            self.assertEqual(bridge.validate_request(self.store, request, PROFILE)[0], "tab-close")
            bridge.reserve_dispatch(self.store, request, PROFILE)
            with self.assertRaises(CleanMacError): bridge.reserve_dispatch(self.store, request, PROFILE)
            changed = dict(request, arguments=dict(arguments, tab={"tab_id": 6}))
            with self.assertRaises(CleanMacError): bridge.validate_request(self.store, changed, PROFILE)
            return {"status": "completed"}
        execute_action_plan(self.store, plan["id"], plan["digest"], "browser-tabs", "tab-close", lambda _: True, perform)
        with self.assertRaises(CleanMacError): bridge.validate_request(self.store, request, PROFILE)

    def test_native_host_real_socket_inspection(self):
        to_host_read, to_host_write = os.pipe(); from_host_read, from_host_write = os.pipe()
        source = os.fdopen(to_host_read, "rb", buffering=0); destination = os.fdopen(from_host_write, "wb", buffering=0)
        input_pipe = os.fdopen(to_host_write, "wb", buffering=0); output_pipe = os.fdopen(from_host_read, "rb", buffering=0)
        errors = []
        def run_host():
            try: bridge.serve_native(self.store, "a" * 32, "chrome-extension://" + "a" * 32 + "/", source=source, destination=destination)
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=run_host, daemon=True); thread.start()
        input_pipe.write(bridge._encoded({"type": "hello", "protocol_version": 1, "profile_id": PROFILE["profile_id"], "label": "Fixture", "extension_id": "a" * 32}))
        profile = bridge.read_message(output_pipe.read)["profile"]
        try:
            described = bridge.call_bridge(self.store, profile["connection"], "describe")
            self.assertEqual(described["label"], "Fixture")
            self.assertEqual(bridge.connections(self.store)[0]["profile_id"], PROFILE["profile_id"])
            result, failure = [], []
            def request_tabs():
                try: result.append(bridge.call_bridge(self.store, profile["connection"], "tabs-inspect", {"max_tabs": 5}))
                except Exception as error: failure.append(error)
            client_thread = threading.Thread(target=request_tabs); client_thread.start()
            request = bridge.read_message(output_pipe.read)
            self.assertEqual(request["command"], "tabs-inspect")
            input_pipe.write(bridge._encoded({"id": request["id"], "ok": True, "result": {**profile, "items": [tab()], "complete": True}}))
            client_thread.join(3)
            self.assertFalse(client_thread.is_alive())
            self.assertEqual(result[0]["items"][0]["tab_id"], 5)
            self.assertFalse(failure)

            arguments = {"profile_id": profile["profile_id"], "session_id": profile["session_id"], "tab": {"tab_id": 5}}
            plan = create_action_plan(self.store, "browser-tabs", "tab-close",
                {"connection": profile["connection"], "command": "tab-close", "arguments": arguments}, "Close fixture tab")
            result.clear()
            def request_close():
                try:
                    result.append(execute_action_plan(self.store, plan["id"], plan["digest"], "browser-tabs", "tab-close", lambda _: True,
                        lambda current: bridge.call_bridge(self.store, profile["connection"], "tab-close", arguments,
                            approval={"plan_id": current["id"], "digest": current["digest"]})))
                except Exception as error: failure.append(error)
            client_thread = threading.Thread(target=request_close); client_thread.start()
            request = bridge.read_message(output_pipe.read)
            self.assertEqual(request["command"], "tab-close")
            input_pipe.write(bridge._encoded({"id": request["id"], "ok": True, "result": {"status": "completed", "tab_id": 5}}))
            client_thread.join(3)
            self.assertFalse(client_thread.is_alive())
            self.assertFalse(failure)
            self.assertEqual(result[0]["status"], "completed")
            with self.assertRaises(CleanMacError):
                bridge.call_bridge(self.store, profile["connection"], "tab-close", arguments, approval={"plan_id": plan["id"], "digest": plan["digest"]})
        finally:
            input_pipe.write(bridge._encoded({"type": "disconnect"})); thread.join(3)
            for stream in (input_pipe, output_pipe, source, destination): stream.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


@unittest.skipUnless(shutil.which("node"), "Node is required to fixture-test the Chrome companion")
class CompanionTests(unittest.TestCase):
    def test_companion_typed_mutations_and_changed_state(self):
        worker = Path(__file__).resolve().parents[1] / "assets/browser-extension/worker.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{webcrypto}=require('crypto');
let changes=[], current={id:5,windowId:9,url:'https://example.test/a',status:'complete',incognito:false,active:false,pinned:false,audible:false,discarded:false,autoDiscardable:true};
const event={addListener(){}};
const chrome={runtime:{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',onMessage:event},storage:{local:{}},windows:{get:async()=>({incognito:false})},
tabs:{onCreated:event,onRemoved:event,onUpdated:event,onReplaced:event,onActivated:event,onAttached:event,onDetached:event,onMoved:event,query:async()=>current?[current]:[],get:async()=>({...current}),remove:async(id)=>{changes.push(['close',id]);current=null},
discard:async(id)=>{changes.push(['discard',id]);current.discarded=true;return {...current}},update:async(id,values)=>{changes.push(['resume',id]);current.discarded=false;return {...current}},
create:async(values)=>{changes.push(['reopen',values]);return {id:6,incognito:false}}},
browsingData:{settings:async()=>({dataRemovalPermitted:{history:true,downloads:true}}),remove:async(options,types)=>changes.push(['privacy',options,types])}};
const context=vm.createContext({chrome,crypto:webcrypto,TextEncoder,URL,console,Map,Set,Date});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const profile={profile_id:'profile',session_id:'session'};
(async()=>{
const hash=await context.hash(current.url), version=context.versionFor(5),args={...profile,tab:{tab_id:5,window_id:9,url_digest:hash,...version},decisions:{allow_form_loss:true}};
await context.handleCommand('tab-discard',args,profile);assert.equal(current.discarded,true);
await context.handleCommand('tab-resume',args,profile);assert.equal(current.discarded,false);
current.pinned=true;await assert.rejects(()=>context.handleCommand('tab-close',args,profile));current.pinned=false;
current.url='https://example.test/b';await assert.rejects(()=>context.handleCommand('tab-close',args,profile));current.url='https://example.test/a';
current.pendingUrl='https://example.test/b';await assert.rejects(()=>context.handleCommand('tab-close',args,profile));delete current.pendingUrl;
await assert.rejects(()=>context.handleCommand('tab-close',{...args,decisions:{}},profile));
await assert.rejects(()=>context.handleCommand('privacy-clear',{...profile,types:['passwords'],since_ms:0},profile));
await assert.rejects(()=>context.handleCommand('privacy-clear',{...profile,types:['history'],since_ms:0,origins:['https://example.test']},profile));
await context.handleCommand('privacy-clear',{...profile,types:['downloads'],since_ms:0},profile);
await context.handleCommand('tab-close',args,profile);assert.equal(current,null);
await assert.rejects(()=>context.handleCommand('tab-reopen',{...profile,url:'javascript:alert(1)',window_id:9},profile));
await assert.rejects(()=>context.handleCommand('tab-reopen',{...profile,url:'https://example.test/?token=secret',window_id:9},profile));
await context.handleCommand('tab-reopen',{...profile,url:'https://example.test/a',window_id:9},profile);
assert.equal(changes.length,5);assert.equal(changes[2][2].downloads,true);console.log('companion fixture passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which("node"), "-e", script, str(worker)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("companion fixture passed", result.stdout)


if __name__ == "__main__": unittest.main()
