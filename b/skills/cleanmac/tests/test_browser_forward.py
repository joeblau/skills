"""Independent browser forward tests: inert Chrome VM and private local sockets.

No real extension is installed, no browser is queried, and all page data is
generated. These tests exercise the transport and dispatch behavior rather than
matching source strings.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cleanmac_core import browser_bridge as bridge
from cleanmac_core.actions import create_action_plan, execute_action_plan
from cleanmac_core.common import CleanMacError
from cleanmac_core.state import StateStore


_NODE_HARNESS = r'''
const fs = require('fs'), vm = require('vm'), {webcrypto} = require('crypto');
const worker = fs.readFileSync(process.argv[1], 'utf8');
function fixture() {
  const events = {}, calls = [], state = {id: 7, windowId: 13, url:'https://example.test/document',
    title:'$(do not execute) <script>fixture</script>', incognito:false, active:false,
    pinned:false, audible:false, discarded:false, autoDiscardable:true, status:'complete'};
  let exists = true, duringHash = null;
  const event = (name) => ({addListener(fn) {events[name] = fn;}});
  const chrome = {
    runtime: {id:'a'.repeat(32), onMessage:event('runtime')},
    storage: {local:{get:async()=>({}), set:async()=>{}}},
    tabs: {
      get:async(id)=>({...state}), query:async()=>exists?[{...state}]:[],
      remove:async(id)=>{calls.push({action:'remove',id}); exists=false;},
      discard:async(id)=>{calls.push({action:'discard',id}); state.discarded=true; return {...state};},
      update:async(id,value)=>{calls.push({action:'update',id,value}); state.active=true; state.discarded=false; return {...state};},
      create:async(value)=>{calls.push({action:'create',value}); return {id:99,incognito:false};},
      onCreated:event('created'), onRemoved:event('removed'), onUpdated:event('updated'),
      onReplaced:event('replaced'), onActivated:event('activated'), onAttached:event('attached'),
      onDetached:event('detached'), onMoved:event('moved')
    },
    windows:{get:async(id)=>({id,incognito:false}), onRemoved:event('windowRemoved')},
    browsingData:{settings:async()=>({dataRemovalPermitted:{}}),
      remove:async(options,types)=>{calls.push({action:'privacy',options,types});}}
  };
  const crypto = {randomUUID:()=>webcrypto.randomUUID(), subtle:{digest:async(...args)=>{
    if (duringHash) { const fn=duringHash; duringHash=null; fn(); }
    return webcrypto.subtle.digest(...args);
  }}};
  const context = vm.createContext({chrome, crypto, TextEncoder, URL, console});
  vm.runInContext(worker, context);
  const profile={profile_id:'12345678-1234-1234-1234-123456789abc',session_id:'s'.repeat(32)};
  return {context,events,calls,state,profile, setHashHook:fn=>duringHash=fn,
    view:()=>context.tabView({...state}),
    command:(name,args)=>context.handleCommand(name,{...profile,...args},profile)};
}
async function outcome(fn) {
  try { return {value:await fn()}; }
  catch(error) { return {error:error.code || error.message}; }
}
'''


@unittest.skipUnless(shutil.which("node"), "Node is needed for inert Chrome companion forward tests")
class BrowserCompanionForwardTests(unittest.TestCase):
    def run_fixture(self, body):
        worker = SCRIPTS.parent / "assets/browser-extension/worker.js"
        process = subprocess.run([shutil.which("node"), "-e", _NODE_HARNESS +
                                  "\n(async()=>{\n" + body +
                                  "\n})().then(value=>process.stdout.write(JSON.stringify(value))).catch(error=>{console.error(error);process.exitCode=1});",
                                  str(worker)], capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_protected_state_changes_during_async_url_check_do_not_close_tab(self):
        results = self.run_fixture(r'''
const results=[];
for (const scenario of ['pinned','audible','active','autoDiscardable','discarded','attached','detached']) {
  const f=fixture(), expected=await f.view();
  f.setHashHook(()=>{
    if (scenario==='active') {
      f.state.active=true;
      if(f.events.activated) f.events.activated({tabId:7,windowId:13});
    } else if(scenario==='attached') {
      f.state.windowId=14;
      if(f.events.attached) f.events.attached(7,{newWindowId:14,newPosition:0});
    } else if(scenario==='detached') {
      f.state.windowId=14;
      if(f.events.detached) f.events.detached(7,{oldWindowId:13,oldPosition:0});
    } else {
      const value=scenario==='autoDiscardable'?false:true;
      f.state[scenario]=value;
      f.events.updated(7,{[scenario]:value},{...f.state});
    }
  });
  const result=await outcome(()=>f.command('tab-close',{tab:expected,decisions:{allow_form_loss:true}}));
  results.push({scenario,...result,calls:f.calls});
}
return results;
''')
        for result in results:
            with self.subTest(scenario=result["scenario"]):
                self.assertEqual(result.get("error"), "browser_target_changed", result)
                self.assertEqual(result["calls"], [], result)

    def test_history_scope_rejection_and_download_history_action_are_distinct(self):
        result = self.run_fixture(r'''
const f=fixture();
const rejected=await outcome(()=>f.command('privacy-clear',{types:['history'],since_ms:1,origins:['https://example.test']}));
const firstCalls=f.calls.slice();
const accepted=await outcome(()=>f.command('privacy-clear',{types:['downloads'],since_ms:12345,origins:[]}));
return {rejected,firstCalls,accepted,calls:f.calls};
''')
        self.assertEqual(result["rejected"]["error"], "browser_action_unavailable")
        self.assertEqual(result["firstCalls"], [])
        self.assertEqual(result["calls"], [{"action": "privacy", "options": {
            "since": 12345, "originTypes": {"unprotectedWeb": True, "protectedWeb": False, "extension": False}},
            "types": {"downloads": True}}])
        self.assertFalse(result["accepted"]["value"]["downloaded_files_changed"])

    def test_reopen_only_preserves_exact_safe_web_target(self):
        results = self.run_fixture(r'''
const results=[];
for(const url of ['javascript:alert(1)','file:///private/data','chrome://settings',
                 'https://user:pass@example.test/path','https://example.test/path?token=private',
                 'https://example.test/path#private','https://example.test/saved']) {
  const f=fixture();
  const result=await outcome(()=>f.command('tab-reopen',{url,window_id:13}));
  results.push({url,...result,calls:f.calls});
}
return results;
''')
        for result in results[:-1]:
            self.assertEqual(result.get("error"), "browser_action_unavailable", result)
            self.assertEqual(result["calls"], [])
        self.assertEqual(results[-1]["calls"], [{"action": "create", "value": {
            "windowId": 13, "url": "https://example.test/saved", "active": False}}])

    def test_private_profile_and_unknown_form_state_never_mutate(self):
        result = self.run_fixture(r'''
const f=fixture(), expected=await f.view();
const forms=await outcome(()=>f.command('tab-close',{tab:expected,decisions:{}}));
f.state.incognito=true;
const privateTab=await outcome(()=>f.command('tab-close',{tab:expected,decisions:{allow_form_loss:true}}));
return {forms,privateTab,calls:f.calls};
''')
        self.assertEqual(result["forms"]["error"], "browser_state_decision_required")
        self.assertEqual(result["privateTab"]["error"], "browser_target_changed")
        self.assertEqual(result["calls"], [])


class BrowserTransportForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cm-browser-forward-", dir="/tmp")
        self.root = Path(self.temp.name).resolve()
        self.store = StateStore(self.root / "state")

    def tearDown(self):
        self.temp.cleanup()

    def test_native_origin_rejected_before_reading_any_handshake(self):
        source = mock.Mock()
        with self.assertRaises(CleanMacError):
            bridge.serve_native(self.store, "a" * 32, "chrome-extension://" + "b" * 32 + "/",
                                source=source, destination=mock.Mock())
        source.read.assert_not_called()

    def test_malicious_framing_cannot_become_a_command(self):
        for raw in (struct.pack("<I", 0), struct.pack("<I", 2**32-1),
                    struct.pack("<I", 11) + b'{"x":NaN}  ',
                    struct.pack("<I", 2) + b"[]"):
            with self.subTest(raw=raw[:4]):
                with self.assertRaises((CleanMacError, EOFError)):
                    bridge.read_message(io.BytesIO(raw).read)

    def test_native_dispatch_timeout_reservation_survives_an_immediate_retry(self):
        incoming_read, incoming_write = os.pipe()
        outgoing_read, outgoing_write = os.pipe()
        source = os.fdopen(incoming_read, "rb", buffering=0)
        destination = os.fdopen(outgoing_write, "wb", buffering=0)
        extension_input = os.fdopen(incoming_write, "wb", buffering=0)
        extension_output = os.fdopen(outgoing_read, "rb", buffering=0)
        host_errors = []
        real_select = select.select

        def fast_timeout(readable, writable, errors, timeout=None):
            # Shorten only the native response wait; no product timing is changed.
            return real_select(readable, writable, errors, .05 if timeout == 40 else timeout)

        def serve():
            try:
                bridge.serve_native(self.store, "a" * 32, "chrome-extension://" + "a" * 32 + "/",
                                    source=source, destination=destination)
            except Exception as exc:
                host_errors.append(exc)

        with mock.patch.object(bridge.select, "select", side_effect=fast_timeout):
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            extension_input.write(bridge._encoded({"type": "hello", "protocol_version": 1,
                "profile_id": "12345678-1234-1234-1234-123456789abc", "label": "Inert fixture", "extension_id": "a" * 32}))
            profile = bridge.read_message(extension_output.read)["profile"]
            # A same-user local caller still cannot transmit malformed frames or
            # perform an unapproved mutation through this private socket.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as untrusted:
                untrusted.settimeout(2)
                untrusted.connect(str(self.store.root / "browser-bridge" / profile["connection"]))
                untrusted.sendall(struct.pack("<I", bridge.MAX_MESSAGE + 1))
                rejected = bridge.read_message(untrusted.recv)
                self.assertFalse(rejected["ok"])
                self.assertEqual(rejected["error"]["code"], "browser_bridge_protocol")
            with self.assertRaises(CleanMacError):
                bridge.call_bridge(self.store, profile["connection"], "tab-close", {}, timeout=2)
            self.assertFalse(real_select([extension_output], [], [], .05)[0])
            args = {"profile_id": profile["profile_id"], "session_id": profile["session_id"],
                    "tab": {"tab_id": 7, "window_id": 13, "url_digest": "a" * 64,
                            "identity": "12345678-1234-1234-1234-123456789abc", "revision": 0},
                    "decisions": {"allow_form_loss": True}}
            plan = create_action_plan(self.store, "browser-tabs", "tab-close",
                                      {"connection": profile["connection"], "command": "tab-close", "arguments": args},
                                      "Close one inert fixture tab")
            proof = {"plan_id": plan["id"], "digest": plan["digest"]}
            dispatched = []

            def execute(current):
                with self.assertRaises(CleanMacError):
                    bridge.call_bridge(self.store, profile["connection"], "tab-close", args, approval=proof, timeout=2)
                self.assertTrue(real_select([extension_output], [], [], 1)[0])
                dispatched.append(bridge.read_message(extension_output.read))
                # Outer receipt is still running here. Independent reservation must stop this retry.
                with self.assertRaises(CleanMacError):
                    bridge.call_bridge(self.store, profile["connection"], "tab-close", args, approval=proof, timeout=2)
                self.assertFalse(real_select([extension_output], [], [], .05)[0])
                raise CleanMacError("Fixture reply deliberately absent", code="browser_bridge_timeout")

            try:
                receipt = execute_action_plan(self.store, plan["id"], plan["digest"], "browser-tabs", "tab-close",
                                              lambda _: True, execute)
                self.assertEqual(receipt["status"], "recovery_needed")
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0]["arguments"], args)
                reservation = self.store.get("receipts", "receipt-browser-" + plan["id"].removeprefix("plan-action-"))
                self.assertEqual(reservation["status"], "running")
                self.assertEqual(execute_action_plan(self.store, plan["id"], plan["digest"], "browser-tabs", "tab-close",
                                                     lambda _: self.fail("revalidated ambiguous mutation"),
                                                     lambda _: self.fail("repeated ambiguous mutation"))["status"], "recovery_needed")
            finally:
                extension_input.write(bridge._encoded({"type": "disconnect"}))
                thread.join(3)
                for stream in (extension_input, extension_output, source, destination):
                    stream.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(host_errors, [])


if __name__ == "__main__":
    unittest.main()
