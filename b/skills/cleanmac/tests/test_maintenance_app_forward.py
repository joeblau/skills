"""Independent CLI journeys using inert executable providers and private fixtures.

System process launches are routed to a single generated fixture binary. Unknown
commands fail the test; no real maintenance, app removal, or account probe runs.
"""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FAKE = r'''
import json, os, pathlib, plistlib, shutil, sys
root = pathlib.Path(__file__).resolve().parent
state_path = root / "provider-state.json"
state = json.loads(state_path.read_text())
command, args = sys.argv[1], sys.argv[2:]
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps({"command":command,"args":args,"env":{k:os.environ.get(k) for k in ("HOMEBREW_CASK_OPTS","HOMEBREW_NO_AUTOREMOVE","HOMEBREW_NO_AUTO_UPDATE","BASH_ENV","RUBYOPT")}})+"\n")
def save(): state_path.write_text(json.dumps(state))
if command == "codesign":
    if "--display" in args:
        print("Identifier=com.example.FixtureApp\nTeamIdentifier=FIXTURETEAM", file=sys.stderr)
    raise SystemExit(0)
if command == "ps":
    raise SystemExit(0)
if command == "brew":
    cask = state["cask"]
    if args == ["--caskroom"]:
        print(str(root / "Caskroom")); raise SystemExit(0)
    if args and args[0] == "info":
        print(json.dumps({"casks": [] if "--installed" in args and not state["installed"] else [cask]})); raise SystemExit(0)
    if args and args[0] == "outdated":
        print(json.dumps({"casks":[{"name":"fixture"}]})); raise SystemExit(0)
    if args == ["uninstall","--cask","fixture"]:
        state["installed"] = False
        shutil.rmtree(root / "Fixture.app")
        save()
        raise SystemExit(1 if state.get("fail_after_effect") else 0)
    if args == ["upgrade","--cask","fixture"]:
        cask["installed"] = cask["version"]
        save()
        raise SystemExit(1 if state.get("fail_after_effect") else 0)
if command == "diskutil" and args[:2] == ["info","-plist"]:
    print(plistlib.dumps({"MountPoint":str(root / "volume"),"VolumeUUID":state["volume_uuid"],"FilesystemType":"apfs","DeviceIdentifier":"disk-fixture","ReadOnlyVolume":False}).decode())
    raise SystemExit(0)
if command == "tmutil":
    if args == ["status"]:
        print("Backup session status = { Running = 0; }"); raise SystemExit(0)
    if args == ["listlocalsnapshots",str(root / "volume")]:
        print("Snapshots for volume " + str(root / "volume") + ":")
        print("\n".join(state["snapshots"]))
        raise SystemExit(0)
    if args == ["deletelocalsnapshots",str(root / "volume")]:
        state["snapshots"] = []
        save()
        raise SystemExit(1 if state.get("fail_after_effect") else 0)
if command == "networksetup":
    if args == ["-listallhardwareports"]:
        print("Hardware Port: Wi-Fi\nDevice: en0"); raise SystemExit(0)
    if args == ["-listpreferredwirelessnetworks","en0"]:
        print("Preferred networks on en0:\n\tFixtureWiFi"); raise SystemExit(0)
    if args == ["-getairportnetwork","en0"]:
        print("Current Wi-Fi Network: FixtureWiFi"); raise SystemExit(0)
if command == "tccutil" and args == ["reset","Camera","com.example.FixtureApp"]:
    print("Successfully reset Camera approval status for com.example.FixtureApp"); raise SystemExit(0)
if command == "open" and args == ["x-apple.systempreferences:com.apple.wifi-settings-extension"]:
    raise SystemExit(0)
print("Unexpected fixture command: " + repr((command,args)), file=sys.stderr)
raise SystemExit(99)
'''

DRIVER = r'''
import os, pathlib, subprocess, sys
root = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, SCRIPTS)
from cleanmac_core import app_tools, maintenance_tools, cli
original = subprocess.Popen
allowed = {"/usr/bin/codesign":"codesign", "/bin/ps":"ps", "/usr/sbin/diskutil":"diskutil", "/usr/bin/tmutil":"tmutil", "/usr/sbin/networksetup":"networksetup", "/usr/bin/tccutil":"tccutil", "/usr/bin/open":"open"}
def route(argv, *args, **kwargs):
    if argv[0] == str(root / "brew"):
        return original([str(root / "fake-provider"),"brew",*argv[1:]],*args,**kwargs)
    if argv[0] in allowed:
        return original([str(root / "fake-provider"),allowed[argv[0]],*argv[1:]],*args,**kwargs)
    raise AssertionError("Unexpected live command blocked: " + str(argv[0]))
subprocess.Popen = route
raise SystemExit(cli.main(sys.argv[1:]))
'''


@unittest.skipUnless(sys.platform == "darwin", "The product CLI gates native operational commands to macOS")
class MaintenanceAppForward(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cleanmac-forward-")
        self.root = Path(self.temp.name).resolve()
        self.volume = self.root / "volume"
        self.volume.mkdir()
        self.app = self.root / "Fixture.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier":"com.example.FixtureApp","CFBundleVersion":"1.0"}))
        self.brew = self.root / "brew"
        self.brew.write_text("#!/bin/sh\nexit 99\n")
        self.brew.chmod(0o700)
        fake = self.root / "fake-provider"
        fake.write_text("#!" + sys.executable + "\n" + FAKE)
        fake.chmod(0o700)
        self.driver = self.root / "driver.py"
        self.driver.write_text("SCRIPTS = " + repr(str(SCRIPTS)) + "\n" + DRIVER)
        self.data = self.root / "cleanmac-state"
        self.provider_state = self.root / "provider-state.json"
        cask = {"token":"fixture","name":["Fixture"],"installed":"1.0","version":"2.0","tap":"homebrew/cask",
                "artifacts":[{"app":["Fixture.app"],"target":str(self.app)}],"sha256":"a"*64,"url":"https://example.com/fixture.zip",
                "depends_on":{},"auto_updates":False}
        self.state = {"cask":cask,"installed":True,"volume_uuid":"11111111-2222-3333-4444-555555555555",
                      "snapshots":["com.apple.TimeMachine.2026-11-13-100000.local","com.apple.TimeMachine.2026-11-13-110000.local"]}
        self.save()
        metadata = self.root / "Caskroom" / "fixture" / ".metadata"
        definitions = metadata / "1.0" / "20261113" / "Casks"
        definitions.mkdir(parents=True)
        self.receipt_path = metadata / "INSTALL_RECEIPT.json"
        self.receipt_path.write_text(json.dumps({"source":{"version":"1.0","tap":"homebrew/cask"},"uninstall_flight_blocks":False,
                                               "uninstall_artifacts":[{"app":["Fixture.app"]}],"runtime_dependencies":{}}))
        (definitions / "fixture.json").write_text(json.dumps({"artifacts":[{"app":["Fixture.app"]}]}))

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.provider_state.write_text(json.dumps(self.state))

    def invoke(self, tool, action="inspect", *, config=None, roots=(), selected=(), record=None, approve=None, env=None, expect=0):
        argv = [sys.executable,str(self.driver),"--json","--data-dir",str(self.data),"tool",tool,"--action",action]
        for root in roots: argv.extend(["--root",str(root)])
        for item in selected: argv.extend(["--select",item])
        if record is not None: argv.extend(["--record",record])
        if approve is not None: argv.extend(["--approve",approve])
        if config is not None: argv.extend(["--config",json.dumps(config)])
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def calls(self):
        file = self.root / "calls.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def app_plan(self, tool="app-uninstall"):
        scan = self.invoke(tool, config={"brew_path":str(self.brew)})
        self.assertEqual(scan["status"], "complete", scan)
        self.assertEqual(len(scan["items"]), 1)
        return self.invoke(tool,"plan",record=scan["id"],selected=[scan["items"][0]["id"]])

    def snapshot_plan(self):
        scan = self.invoke("snapshots", roots=[self.volume])
        return self.invoke("snapshots","plan", roots=[self.volume], selected=[v["id"] for v in scan["snapshots"]],config={"operation":"delete-volume-snapshots"})

    def test_app_cli_explicit_approval_and_environment_scope(self):
        plan = self.app_plan()
        denied = self.invoke("app-uninstall","apply",record=plan["id"],approve="wrong",expect=2)
        self.assertEqual(denied["error"]["code"],"approval_required")
        self.assertTrue(self.app.exists())
        env = dict(os.environ,HOMEBREW_CASK_OPTS="--zap --force --appdir=/tmp/other",HOMEBREW_NO_AUTO_UPDATE="0",BASH_ENV="/tmp/injected",RUBYOPT="-r/tmp/injected")
        receipt = self.invoke("app-uninstall","apply",record=plan["id"],approve=plan["digest"],env=env)
        self.assertEqual(receipt["status"],"completed",receipt)
        mutation = [c for c in self.calls() if c["command"] == "brew" and c["args"][0] == "uninstall"]
        self.assertEqual(len(mutation),1)
        self.assertEqual(mutation[0]["args"],["uninstall","--cask","fixture"])
        self.assertIsNone(mutation[0]["env"]["HOMEBREW_CASK_OPTS"])
        self.assertIsNone(mutation[0]["env"]["BASH_ENV"])
        self.assertIsNone(mutation[0]["env"]["RUBYOPT"])
        self.assertEqual(mutation[0]["env"]["HOMEBREW_NO_AUTOREMOVE"],"1")
        self.assertFalse(self.app.exists())

    def test_app_cli_post_effect_failure_never_repeats(self):
        plan = self.app_plan()
        self.state["fail_after_effect"] = True
        self.save()
        receipt = self.invoke("app-uninstall","apply",record=plan["id"],approve=plan["digest"])
        self.assertEqual(receipt["status"],"recovery_needed")
        self.assertFalse(self.app.exists())
        repeated = self.invoke("app-uninstall","apply",record=plan["id"],approve=plan["digest"])
        self.assertEqual(repeated["status"],"recovery_needed")
        self.assertEqual(len([c for c in self.calls() if c["command"] == "brew" and c["args"][0] == "uninstall"]),1)

    def test_app_cli_installed_uninstall_hook_blocks_plan(self):
        receipt = json.loads(self.receipt_path.read_text())
        receipt["uninstall_flight_blocks"] = True
        self.receipt_path.write_text(json.dumps(receipt))
        scan = self.invoke("app-uninstall",config={"brew_path":str(self.brew)})
        failed = self.invoke("app-uninstall","plan",record=scan["id"],selected=[scan["items"][0]["id"]],expect=2)
        self.assertEqual(failed["error"]["code"],"unsupported_cask")
        self.assertTrue(self.app.exists())

    def test_app_cli_dependency_expansion_not_eligible(self):
        self.state["cask"]["depends_on"] = {"formula":["unselected-dependency"]}
        self.save()
        scan = self.invoke("app-updates",config={"brew_path":str(self.brew)})
        self.assertFalse(scan["items"][0]["eligible"])
        failed = self.invoke("app-updates","plan",record=scan["id"],selected=[scan["items"][0]["id"]],expect=2)
        self.assertEqual(failed["error"]["code"],"invalid_selection")

    def test_app_provider_url_credentials_are_not_reported_or_saved(self):
        self.state["cask"]["url"] = "https://user:fixture-secret@example.com/Fixture.zip"
        self.save()
        scan = self.invoke("app-updates",config={"brew_path":str(self.brew)})
        self.assertFalse(scan["items"][0]["eligible"])
        self.assertNotIn("fixture-secret",json.dumps(scan))
        for record in self.data.glob("scans/*.json"):
            self.assertNotIn("fixture-secret",record.read_text())

    def test_snapshot_cli_binds_entire_selected_volume_pool(self):
        scan = self.invoke("snapshots",roots=[self.volume])
        failed = self.invoke("snapshots","plan",roots=[self.volume],selected=[scan["snapshots"][0]["id"]],config={"operation":"delete-volume-snapshots"},expect=2)
        self.assertEqual(failed["error"]["code"],"invalid_selection")
        plan = self.snapshot_plan()
        self.invoke("snapshots","apply",record=plan["id"],approve="incorrect",expect=2)
        self.assertFalse(any(c["args"][0] == "deletelocalsnapshots" for c in self.calls() if c["command"] == "tmutil"))
        receipt = self.invoke("snapshots","apply",record=plan["id"],approve=plan["digest"])
        self.assertEqual(receipt["status"],"completed")
        self.assertEqual(len(receipt["result"]["removed_snapshots"]),2)
        commands = [c["args"] for c in self.calls() if c["command"] == "tmutil" and c["args"][0] == "deletelocalsnapshots"]
        self.assertEqual(commands,[["deletelocalsnapshots",str(self.volume)]])

    def test_snapshot_cli_replaced_mount_refuses_action(self):
        plan = self.snapshot_plan()
        self.state["volume_uuid"] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
        self.save()
        failed = self.invoke("snapshots","apply",record=plan["id"],approve=plan["digest"],expect=2)
        self.assertEqual(failed["error"]["code"],"volume_changed")
        self.assertFalse(any(c["args"][0] == "deletelocalsnapshots" for c in self.calls() if c["command"] == "tmutil"))

    def test_snapshot_cli_error_after_effect_is_not_retried(self):
        plan = self.snapshot_plan()
        self.state["fail_after_effect"] = True
        self.save()
        first = self.invoke("snapshots","apply",record=plan["id"],approve=plan["digest"])
        self.assertEqual(first["status"],"recovery_needed")
        again = self.invoke("snapshots","apply",record=plan["id"],approve=plan["digest"])
        self.assertEqual(again["status"],"recovery_needed")
        self.assertEqual(len([c for c in self.calls() if c["command"] == "tmutil" and c["args"][0] == "deletelocalsnapshots"]),1)

    def test_wifi_cli_handoff_does_not_claim_settings_changed(self):
        scan = self.invoke("wifi")
        plan = self.invoke("wifi","plan",selected=[scan["networks"][0]["id"]],config={"operation":"open-settings"})
        receipt = self.invoke("wifi","apply",record=plan["id"],approve=plan["digest"])
        self.assertFalse(receipt["result"]["data_changed_by_tool"])
        verified = self.invoke("wifi","verify",record=receipt["id"])
        self.assertEqual(verified["outcome"],"unchanged")
        self.assertEqual([c["args"] for c in self.calls() if c["command"] == "open"],[["x-apple.systempreferences:com.apple.wifi-settings-extension"]])

    def test_cli_permission_reset_is_one_service_one_app(self):
        rejected = self.invoke("app-permissions","plan",config={"operation":"reset","service":"All","bundle_id":"com.example.FixtureApp"},expect=2)
        self.assertEqual(rejected["error"]["code"],"invalid_service")
        plan = self.invoke("app-permissions","plan",config={"operation":"reset","service":"Camera","bundle_id":"com.example.FixtureApp"})
        receipt = self.invoke("app-permissions","apply",record=plan["id"],approve=plan["digest"])
        self.assertEqual(receipt["status"],"completed")
        self.assertEqual(receipt["result"]["grant_state"],"unknown")
        self.assertEqual([c["args"] for c in self.calls() if c["command"] == "tccutil"],[["reset","Camera","com.example.FixtureApp"]])

    def test_cli_rejects_command_like_hostname_before_provider(self):
        marker = self.root / "must-not-exist"
        failed = self.invoke("dns",config={"hostname":"$(touch " + str(marker) + ")"},expect=2)
        self.assertEqual(failed["error"]["code"],"invalid_hostname")
        self.assertFalse(marker.exists())
        self.assertEqual(self.calls(),[])

    def test_permission_diagnosis_missing_selected_root_is_partial(self):
        result = self.invoke("disk-permissions",roots=[self.root / "missing"])
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["locations"][0]["coverage"],"unscanned")


if __name__ == "__main__":
    unittest.main()
