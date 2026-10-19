"""Primary source bodies are evidence, not optional snippets or executable HTML."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools.quant_research_loop import qa_review as Q


class SourceQATests(unittest.TestCase):
    def fixture(self, root, inputs):
        folder = root / "job"
        (folder / "work").mkdir(parents=True)
        (folder / "job.json").write_text(json.dumps({"job_id": "job", "inputs": inputs}), encoding="utf-8")
        (folder / "work/analysis.py").write_text("# source", encoding="utf-8")
        (folder / "work/result.json").write_text('{"payload":{"market_rows_read":0}}', encoding="utf-8")
        entry = root / "tools/quant_research_loop/generic_entry.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# entry", encoding="utf-8")
        state = {"qa_authority": {}, "completed": {"job": {}}}
        return state, folder

    def test_complete_pinned_source_text_and_calendar_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = []
            for name, text in (("paper.txt", "FULL PAPER\nnot a selected excerpt"),
                               ("calendar.html", '<html><a href="monetary20230101a.htm">full calendar</a></html>')):
                path = root / name
                path.write_text(text, encoding="utf-8")
                inputs.append({"path": name, "sha256": Q.digest(path), "kind": "text", "partition": "POLICY_OR_METADATA"})
            state, folder = self.fixture(root, inputs)
            with patch.object(Q, "ROOT", root):
                bundle = Q.build_bundle(state, folder)
            for item in inputs:
                self.assertIn(item["path"], bundle["required_complete_files"])
                self.assertEqual(bundle["files"][item["path"]]["content"], (root / item["path"]).read_text(encoding="utf-8"))
                self.assertFalse(bundle["files"][item["path"]]["truncated"])

    def test_tampered_direct_source_pin_fails_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "paper.txt"
            source.write_text("original", encoding="utf-8")
            inputs = [{"path": "paper.txt", "sha256": Q.digest(source), "kind": "text", "partition": "POLICY_OR_METADATA"}]
            state, folder = self.fixture(root, inputs)
            source.write_text("changed", encoding="utf-8")
            with patch.object(Q, "ROOT", root), self.assertRaisesRegex(ValueError, "identity changed"):
                Q.build_bundle(state, folder)

    def test_oversized_source_remains_required_and_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "paper.txt"
            source.write_text("x" * 600000, encoding="utf-8")
            inputs = [{"path": "paper.txt", "sha256": Q.digest(source), "kind": "text", "partition": "POLICY_OR_METADATA"}]
            state, folder = self.fixture(root, inputs)
            with patch.object(Q, "ROOT", root):
                bundle = Q.build_bundle(state, folder)
            self.assertIn("paper.txt", bundle["required_complete_files"])
            self.assertTrue(bundle["files"]["paper.txt"]["truncated"])

    def test_market_csv_is_not_added_as_source_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "prices.csv"
            source.write_text("DO NOT LOAD PRICE INPUT", encoding="utf-8")
            inputs = [{"path": "prices.csv", "sha256": Q.digest(source), "kind": "csv", "partition": "train"}]
            state, folder = self.fixture(root, inputs)
            with patch.object(Q, "ROOT", root):
                bundle = Q.build_bundle(state, folder)
            self.assertNotIn("prices.csv", bundle["files"])

    def test_addendum_requires_existing_exact_metadata_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, folder = self.fixture(root, [])
            proof = root / "proof.json"
            proof.write_text('{"original_grant":true}', encoding="utf-8")
            ref = {"path": "proof.json", "sha256": Q.digest(proof)}
            state["qa_evidence_addenda"] = {"job": [ref]}
            with patch.object(Q, "ROOT", root), self.assertRaisesRegex(ValueError, "metadata authority"):
                Q.build_bundle(state, folder)
            state["authorized_metadata_artifacts"] = {"proof.json": {"sha256": ref["sha256"], "partition": "POLICY_OR_METADATA"}}
            with patch.object(Q, "ROOT", root):
                bundle = Q.build_bundle(state, folder)
            self.assertIn("proof.json", bundle["required_complete_files"])
            self.assertFalse(bundle["files"]["proof.json"]["truncated"])
            state["authorized_metadata_artifacts"]["proof.json"]["sha256"] = "changed"
            with patch.object(Q, "ROOT", root), self.assertRaisesRegex(ValueError, "metadata authority"):
                Q.build_bundle(state, folder)


if __name__ == "__main__":
    unittest.main()
