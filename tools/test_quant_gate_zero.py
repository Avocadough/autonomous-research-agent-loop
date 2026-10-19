"""Regression checks for real gate transport, independent of model prose."""
import json
import unittest

from quant_research_loop.gate_zero import final_worker_json, env_clean, native_command, invoke


class GateTransportTests(unittest.TestCase):
    def test_commentary_is_not_final_result(self):
        raw = '\n'.join(json.dumps(e) for e in [
            {"type": "text", "part": {"text": "Reading the evidence now"}},
            {"type": "tool_use", "part": {"text": "arbitrary tool output"}},
            {"type": "text", "part": {"text": '{"total_net_usd":-55}'}}])
        self.assertEqual(final_worker_json(raw), {"total_net_usd": -55})

    def test_malformed_final_never_keyword_pass(self):
        with self.assertRaises(ValueError):
            final_worker_json(json.dumps({"type": "text", "part": {"text": "PASS"}}))

    def test_missing_final_fails(self):
        with self.assertRaises(ValueError):
            final_worker_json('{"type":"step_start"}')

    def test_paid_env_not_forwarded(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'dummy', 'OPENAI_BASE_URL': 'dummy'}):
            self.assertNotIn('OPENAI_API_KEY', env_clean())
            self.assertNotIn('OPENAI_BASE_URL', env_clean())

    def test_no_cmd_shim(self):
        self.assertFalse(native_command('codex')[0].lower().endswith('.cmd'))
        self.assertFalse(native_command('opencode')[0].lower().endswith('.cmd'))

    def test_long_unicode_stdin_and_spaces(self):
        import tempfile, sys
        from pathlib import Path
        prompt=('ข้อความ "quoted" $literal `backtick`\n'*2000)
        with tempfile.TemporaryDirectory(prefix='payout ไทย spaces ') as d:
            script=Path(d)/'stdin reader.py'
            script.write_text('import sys,json\ns=sys.stdin.read()\nprint(json.dumps({"length":len(s)}))',encoding='utf-8')
            receipt,raw=invoke([sys.executable,'-X','utf8',str(script)],prompt,Path(d)/'call')
            self.assertEqual(receipt['exit_code'],0)
            self.assertEqual(json.loads(raw)['length'],len(prompt))


if __name__ == '__main__':
    unittest.main()
