"""Task 5B: the frozen prompt set (prompts/MANIFEST.json)."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.prompt_manifest import build_manifest, file_hashes, set_hash, verify_manifest

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
PINNED_V1 = {   # sha256 of the v1 files as committed before Task 5B
    "judge_modality.v1.txt": "3676ebfcee64b0a0c5af869436f9fcb5bb51e7f14f4864ca26e82a9bff546414",
    "prior_only.p0.v1.txt": "45209203d037cbe11cb7413e120b89ee766e5b04c7074aee57515577592b115a",
    "prior_only.p1.v1.txt": "9faf1e308ff5722fbeae37e55e89b4eb6c942d08d6f7077bd5589a913a496738",
    "prior_only.p2.v1.txt": "f24c547ee049f02592ec0046cd7c789efe591b0c3c4850bfa35adfeb81840059",
    "reconstruct.p0.v1.txt": "4b17dd7827b2de459229e931a4195efc5cb2f256d1cd8120235a46035c43584a",
    "reconstruct.p1.v1.txt": "2441bfc9d66e4e5846f870d675bb22cd52be28b4d86b34cfe632f16a9eab998e",
    "reconstruct.p2.v1.txt": "0e6285df851fe4c4c9a5e9e1d7fef6c9976fb323b5ca475a9fb0b3c5d1158f2d",
}


class TestManifest(unittest.TestCase):
    def test_manifest_matches_the_files_on_disk(self):
        self.assertEqual(verify_manifest(PROMPTS), [])

    def test_manifest_lists_every_prompt_file_and_a_set_hash(self):
        m = json.loads((PROMPTS / "MANIFEST.json").read_text(encoding="utf-8"))
        on_disk = sorted(p.name for p in PROMPTS.iterdir() if p.is_file() and p.name != "MANIFEST.json")
        self.assertEqual(sorted(m["files"]), on_disk)
        for name in ("reconstruct.p0.v2.txt", "reconstruct.p1.v2.txt", "reconstruct.p2.v2.txt",
                     "prior_only.p0.v2.txt", "prior_only.p1.v2.txt", "prior_only.p2.v2.txt",
                     "system.reconstruct.v2.txt", "system.judge.v2.txt", "judge_modality.v2.txt",
                     "definitions.v2.txt", "reconstruct.p0.v1.txt", "judge_modality.v1.txt",
                     "align.v2.txt", "in_transcript.v2.txt", "canonicalize.v2.txt", "confirm_mapping_talk.v2.txt",
                     "system.helper.v2.txt", "align.v1.txt", "in_transcript.v1.txt", "canonicalize.v1.txt",
                     "confirm_mapping_talk.v1.txt"):
            self.assertIn(name, m["files"])
        self.assertEqual(m["set_sha256"], set_hash(m["files"]))
        self.assertEqual(len(m["set_sha256"]), 64)

    def test_any_edit_addition_or_removal_is_visible(self):
        d = Path(tempfile.mkdtemp())
        for p in PROMPTS.iterdir():
            if p.is_file():
                (d / p.name).write_bytes(p.read_bytes())
        self.assertEqual(verify_manifest(d), [])
        before = build_manifest(d)["set_sha256"]
        f = d / "reconstruct.p1.v2.txt"
        f.write_text(f.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.assertTrue(any("reconstruct.p1.v2.txt" in x for x in verify_manifest(d)))
        self.assertNotEqual(build_manifest(d)["set_sha256"], before)
        f.write_text(f.read_text(encoding="utf-8")[:-1], encoding="utf-8")
        self.assertEqual(verify_manifest(d), [])
        (d / "extra.v2.txt").write_text("x", encoding="utf-8")
        self.assertTrue(any("extra.v2.txt" in x for x in verify_manifest(d)))
        (d / "extra.v2.txt").unlink()
        (d / "definitions.v2.txt").unlink()
        self.assertTrue(any("definitions.v2.txt" in x for x in verify_manifest(d)))

    def test_dotfiles_are_ignored(self):
        d = Path(tempfile.mkdtemp())
        for p in PROMPTS.iterdir():
            if p.is_file():
                (d / p.name).write_bytes(p.read_bytes())
        (d / ".DS_Store").write_bytes(b"\x00junk")
        (d / ".hidden.txt").write_text("x", encoding="utf-8")
        self.assertEqual(verify_manifest(d), [])
        self.assertNotIn(".DS_Store", file_hashes(d))
        self.assertFalse([n for n in json.loads((PROMPTS / "MANIFEST.json").read_text())["files"]
                          if n.startswith(".")])

    def test_missing_manifest_is_reported(self):
        self.assertEqual(verify_manifest(tempfile.mkdtemp()), ["MANIFEST.json is missing"])

    def test_v1_files_are_unchanged_since_the_record(self):
        # the v1 wordings produced the smoke baseline; their hashes are pinned here as well as in the manifest
        h = file_hashes(PROMPTS)
        for name, sha in PINNED_V1.items():
            self.assertEqual(h[name], sha, name)


if __name__ == "__main__":
    unittest.main()
