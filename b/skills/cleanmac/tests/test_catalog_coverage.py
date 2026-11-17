from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from cleanmac_core.extensions import catalog
from cleanmac_core.permissions import _FEATURE_SUPPORT


class CatalogCoverageTests(unittest.TestCase):
    def test_every_roadmap_area_has_an_installed_workflow_or_core_handler(self):
        document = json.loads((SKILL / "references" / "features.json").read_text())
        identifiers = {feature["id"] for feature in document["features"]}
        self.assertEqual(identifiers, {f"CM-{index:03d}" for index in range(1, 77)})
        result = catalog()
        self.assertEqual(result["unavailable_modules"], [])
        routed = set(_FEATURE_SUPPORT)
        for tool in result["tools"]:
            self.assertTrue(tool["actions"])
            self.assertTrue(set(tool["feature_ids"]) <= identifiers)
            routed.update(tool["feature_ids"])
        self.assertEqual(identifiers - routed, set())

    def test_discovery_never_connects_accounts_or_probes_native_permissions(self):
        with patch("subprocess.run", side_effect=AssertionError("process from discovery")), patch("subprocess.Popen", side_effect=AssertionError("worker from discovery")), patch("urllib.request.urlopen", side_effect=AssertionError("network from discovery")), patch("socket.socket", side_effect=AssertionError("socket from discovery")):
            result = catalog()
        self.assertEqual(result["unavailable_modules"], [])
        self.assertTrue(result["tools"])
        for tool in result["tools"]:
            self.assertEqual(tool["dependency_status"], "not-checked")


if __name__ == "__main__":
    unittest.main()
