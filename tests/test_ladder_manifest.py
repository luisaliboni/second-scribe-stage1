"""Ladder item 3: the two ladder prompt files are in prompts/MANIFEST.json and the manifest check passes."""
import hashlib
import json
import unittest
from pathlib import Path

from second_scribe.prompt_manifest import verify_manifest

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
V2 = "reconstruct.p0.v2.txt"
REMEDY = "reconstruct.p0.remedy.v1.txt"
PLACEBO = "reconstruct.p0.placebo.v1.txt"


class TestManifestHasTheLadderFiles(unittest.TestCase):
    def test_manifest_lists_both_new_files_and_the_check_passes(self):
        manifest = json.loads((PROMPTS / "MANIFEST.json").read_text(encoding="utf-8"))
        for name in (REMEDY, PLACEBO):
            self.assertIn(name, manifest["files"])
            self.assertEqual(manifest["files"][name], hashlib.sha256((PROMPTS / name).read_bytes()).hexdigest())
        self.assertEqual(verify_manifest(PROMPTS), [])

    def test_existing_entries_were_not_rewritten(self):
        # every file that was in the manifest before the ladder files keeps its recorded hash
        manifest = json.loads((PROMPTS / "MANIFEST.json").read_text(encoding="utf-8"))["files"]
        # reconstruct.p0.v2.txt changed once on purpose in run 2 (15 Oct 2026: the turn sentence and the node-type
        # sentence); its run-1 sha256 was 24f61a31fbce16985abd141bd965a21a8e40e9600e930bd0e95bcbf44cd591f5
        self.assertEqual(manifest[V2], "831b51122594ed313f81b5b818a2406153a5c1fb439348930cc61bf7804494a5")
        self.assertEqual(manifest["system.reconstruct.v2.txt"],
                         "deb5362a70a396fcb35fcff06fa6b12170253bf50736673b5dbeaafd03ead166")
        self.assertEqual(manifest["definitions.v2.txt"],
                         "306439dec9d09303ca3ccc877962868b60596741dac96d9b15a135821cdcbc34")


if __name__ == "__main__":
    unittest.main()
