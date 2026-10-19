import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.quant_research_loop import skill_engineering as S


def skill(name, body='Use the existing frozen task and preserve its scope.'):
    return f'---\nname: {name}\ndescription: "Bounded workflow guidance for tests."\n---\n\n{body}\n'


class SkillEngineeringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(S, 'ROOT', self.root).start()
        patch.object(S, '_source_roots', return_value=[self.root / '.codex/skills', self.root / 'installed']).start()
        self.fixed = self.root / S.PROTECTED
        self.fixed.parent.mkdir(parents=True)
        self.fixed.write_text(skill('payout-skill-engineering'), encoding='utf-8')
        self.source = self.root / 'installed/writing-plans/SKILL.md'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(skill('writing-plans'), encoding='utf-8')
        self.source_ref = {'path': 'installed/writing-plans/SKILL.md', 'sha256': S.digest(self.source)}
        base = self.root / S.AUTHORIZATION
        base.parent.mkdir(parents=True)
        user = base.parent / 'USER_AUTHORIZATION.txt'
        user.write_text('Every 10 calls, maintain scoped project skills with separate QA.', encoding='utf-8')
        auth = {
            'revision_id': S.REVISION, 'campaign_id': 'PAYOUT_V1', 'approved': True,
            'source': user.name, 'source_sha256': S.digest(user), 'provider': 'codex', 'login': 'chatgpt',
            'model': S.MODEL, 'reasoning': 'high', 'interval_calls': 10, 'global_call_cap': 1000,
            'spend_usd': 0, 'live': 'DISABLED', 'tools_allowed': False, 'resume_authorized': False,
            'managed_skill_pattern': '.codex/skills/payout-*/SKILL.md', 'protected_skill': S.PROTECTED,
            'max_updates': 3, 'max_skill_chars': 16000, 'max_selected_per_role': 3,
            'role_skill': {'path': S.PROTECTED, 'sha256': S.digest(self.fixed)},
        }
        S.write(base, auth)
        self.state = {'status': 'RUNNING', 'model_calls': 527, 'bootstrap_invocations': 1,
                      'prior_reserved_calls': 12, 'active_invocation_s': 0,
                      'skill_engineering_authority': dict(S.pin(base), revision_id=S.REVISION),
                      'skill_engineering': {'interval_calls': 10, 'next_review_at': 540,
                                           'reviews': {}, 'active_skills': {}},
                      'pending_job': {'job_id': 'TASK', 'job_class': 'ENGINEERING', 'objective': 'repair known workflow'}}
        self.proposal = {'threshold': 540, 'summary': 'No unjustified changes.',
                         'selected_skills': {role: [] for role in S.ROLES}, 'updates': [], 'limitations': []}
        outer = self
        class Controller:
            def __init__(self):
                self.state = outer.state
                self.profile = {'max_calls': 1000}
                self.calls = []
                self.after_call = None
                self.omit_output = False
                self.interrupt = False
                self.qa_verdict = 'APPROVE'
                self.events_extra = []
            def save(self):
                pass
            def model_call(self, cmd, prompt, folder, **kwargs):
                self.calls.append((cmd, prompt, folder))
                self.state['model_calls'] += 1
                self.state['inflight_call'] = folder.relative_to(outer.root).as_posix()
                folder.mkdir(parents=True)
                S.write(folder / 'request.json', {'argv': cmd, 'cwd': str(kwargs['cwd'])})
                (folder / 'prompt.txt').write_text(prompt, encoding='utf-8')
                if folder.name == 'call_qa':
                    evidence = json.loads(prompt.split('\nEVIDENCE=', 1)[1])
                    value = {'threshold': 540, 'proposal_sha256': evidence['proposal_sha256'],
                             'verdict': self.qa_verdict, 'findings': []}
                else:
                    value = copy.deepcopy(outer.proposal)
                response = Path(cmd[cmd.index('-o') + 1])
                if not self.omit_output:
                    S.write(response, value)
                events = [{'type': 'thread.started', 'thread_id': 'offline-fixture'},
                          {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(value)}},
                          *self.events_extra, {'type': 'turn.completed'}]
                raw = '\n'.join(json.dumps(event) for event in events)
                receipt = {'exit_code': 0, 'elapsed_s': 1.25}
                S.write(folder / 'receipt.json', receipt)
                (folder / 'stdout.jsonl').write_text(raw, encoding='utf-8')
                (folder / 'stderr.txt').write_text('', encoding='utf-8')
                if self.interrupt:
                    raise RuntimeError('simulated crash after raw receipt before accounting')
                self.state['active_invocation_s'] += receipt['elapsed_s']
                self.state.pop('inflight_call')
                if self.after_call:
                    self.after_call(folder)
                return receipt, raw
        self.controller = Controller()

    def update(self, name='payout-repair'):
        path = f'.codex/skills/{name}/SKILL.md'
        self.proposal['updates'].append({'path': path, 'expected_sha256': None,
                                        'content': skill(name), 'reason': 'Observed repeated scoped repair need.',
                                        'source_refs': [self.source_ref]})
        self.proposal['selected_skills']['executor'].append(path)
        return self.root / path

    def test_authority_pins_user_and_fixed_role_and_preserves_permission(self):
        self.assertEqual(S.authority(self.state)['model'], S.MODEL)
        self.fixed.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            S.authority(self.state)

    def test_noop_cadence_counts_engineer_call_and_never_recurses(self):
        result = S.maybe_review(self.controller)
        self.assertEqual(result['completed_used_call_units'], 541)
        self.assertEqual(self.state['skill_engineering']['next_review_at'], 550)
        self.assertEqual(len(self.controller.calls), 1)
        cmd, prompt, _ = self.controller.calls[0]
        self.assertEqual(cmd[cmd.index('-m') + 1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertIn('No tools', prompt)
        self.assertIn('A no-change review is valid every ten calls', prompt)
        self.assertIn('never style preferences', prompt)
        self.assertIn('the Executor chooses implementation', prompt)
        self.assertIn('not code or patch recipes', prompt)
        self.assertIsNone(S.maybe_review(self.controller))
        self.assertEqual(len(self.controller.calls), 1)

    def test_late_boundary_coalesces_thresholds_without_backlog_calls(self):
        self.state['model_calls'] += 23
        result = S.maybe_review(self.controller)
        self.assertEqual(result['coalesced_thresholds'], [540, 550, 560])
        self.assertEqual(result['started_used_call_units'], 563)
        self.assertEqual(result['completed_used_call_units'], 564)
        self.assertEqual(result['next_review_at'], 570)
        self.assertEqual(len(self.controller.calls), 1)

    def test_waits_for_safe_boundary_and_defers_nested_holds(self):
        for change in ({'status': 'HUMAN_ACTION_REQUIRED'}, {'inflight_call': 'other'},
                       {'operator_safety_hold': {'active': False, 'prior_unreleased_hold': {'active': True}}},
                       {'operator_auxiliary_incidents': {'incident': {'resolved': False}}}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.state)
                self.state.update(change)
                self.assertEqual(S.maybe_review(self.controller)['status'], 'DEFERRED')
                self.state.clear()
                self.state.update(original)
        self.assertEqual(self.controller.calls, [])

    def test_reserves_engineer_qa_and_next_cycle_without_spending(self):
        self.state['model_calls'] = 985  # 998 total; only two units remain.
        with self.assertRaisesRegex(RuntimeError, 'mandatory next cycle'):
            S.maybe_review(self.controller)
        self.assertEqual(self.state['status'], 'RESOURCE_LIMIT')
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.state['model_calls'], 985)

    def test_incomplete_saved_worker_scope_defers_all_new_skill_artifacts(self):
        folder = self.root / S.RUNTIME / 'jobs/TASK'
        for attempt in ('0', 'recovery_0'):
            with self.subTest(attempt=attempt):
                call = folder / ('worker_call_' + attempt)
                call.mkdir(parents=True)
                before = folder / ('scope_before_' + attempt + '.json')
                S.write(before, {'protected': 'hash'})
                self.assertEqual(S.maybe_review(self.controller)['status'], 'DEFERRED')
                self.assertFalse((self.root / S.RUNTIME / 'skill_engineering').exists())
                self.assertEqual(self.controller.calls, [])
                S.write(folder / ('scope_check_' + attempt + '.json'), {'changed': []})
                self.assertTrue(S._safe(self.state))

    def test_existing_selection_delivers_pinned_full_content_and_detects_drift(self):
        self.proposal['selected_skills']['lead'] = [self.source_ref['path']]
        S.maybe_review(self.controller)
        guidance = S.guidance(self.state, 'lead')
        self.assertEqual(guidance['skills'][0]['content'], self.source.read_text(encoding='utf-8'))
        self.assertTrue(guidance['advisory'])
        self.assertEqual(guidance['threshold'], 540)
        self.source.write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed after review'):
            S.guidance(self.state, 'lead')

    def test_approved_project_update_uses_independent_second_call_and_no_global_edit(self):
        target = self.update()
        original_source = self.source.read_bytes()
        result = S.maybe_review(self.controller)
        self.assertEqual(len(self.controller.calls), 2)
        self.assertIn('independent Skill Engineering reviewer', self.controller.calls[1][1])
        self.assertIn('reject style-only instruction growth', self.controller.calls[1][1])
        self.assertIn('without a decision-changing need', self.controller.calls[1][1])
        self.assertEqual(result['completed_used_call_units'], 542)
        self.assertTrue(target.exists())
        self.assertEqual(self.source.read_bytes(), original_source)
        self.assertEqual(S.guidance(self.state, 'executor')['skills'][0]['content'], target.read_text(encoding='utf-8'))

    def test_rejected_update_stays_charged_without_mutation_or_automatic_retry(self):
        target = self.update()
        self.controller.qa_verdict = 'REJECT'
        with self.assertRaisesRegex(ValueError, 'QA rejected'):
            S.maybe_review(self.controller)
        self.assertFalse(target.exists())
        self.assertEqual(len(self.controller.calls), 2)
        with self.assertRaisesRegex(ValueError, 'prior review blocked'):
            S.maybe_review(self.controller)
        self.assertEqual(len(self.controller.calls), 2)

    def test_unseen_source_cannot_be_adapted_and_scope_escape_is_rejected(self):
        self.update()
        evidence = S._evidence(self.state, 540)
        for item in evidence['skill_catalog']:
            if item['path'] == self.source_ref['path']:
                item['body_included'] = False
        with self.assertRaisesRegex(ValueError, 'source body supplied'):
            S._proposal(self.proposal, evidence)
        for path in (S.PROTECTED, '../skills/payout-escape/SKILL.md',
                     '.codex/skills/payout-repair/script.py', '.opencode/skills/payout-repair/SKILL.md'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'outside managed'):
                S.managed_path(path)

    def test_managed_frontmatter_is_valid_and_folder_name_matches(self):
        path = '.codex/skills/payout-repair/SKILL.md'
        S.validate_skill(skill('payout-repair'), path)
        for content in (skill('other-name'),
                        '---\nname: payout-repair\nname: payout-repair\n---\nbody',
                        "---\nname: payout-repair\ndescription: 'unclosed\n---\nbody",
                        '---\nname: payout-repair\ndescription: [broken\n---\nbody'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                S.validate_skill(content, path)

    def test_managed_symlink_cannot_escape_scope(self):
        outside = self.root / 'outside'
        outside.mkdir()
        linked = self.root / '.codex/skills/payout-linked'
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest('Host cannot create a test symlink: ' + str(error))
        with self.assertRaisesRegex(ValueError, 'symlink/junction'):
            S.managed_path('.codex/skills/payout-linked/SKILL.md')

    def test_partial_apply_rolls_forward_exact_plan_without_repeat_model_calls(self):
        first, second = self.update('payout-one'), self.update('payout-two')
        original = S._replace_skill
        seen = []
        def crash_second(path, blob, temporary):
            seen.append(path)
            if len(seen) == 2:
                raise OSError('simulated interruption')
            return original(path, blob, temporary)
        with patch.object(S, '_replace_skill', side_effect=crash_second):
            with self.assertRaisesRegex(S.InterruptedSkillApply, 'rollforward'):
                S.maybe_review(self.controller)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(self.state['skill_engineering']['reviews']['540']['phase'], 'RECONCILE')
        result = S.maybe_review(self.controller)
        self.assertTrue(second.exists())
        self.assertEqual(len(self.controller.calls), 2)
        self.assertEqual(result['completed_used_call_units'], 542)

    def test_all_apply_preconditions_checked_before_first_write(self):
        first, second = self.update('payout-one'), self.update('payout-two')
        def concurrent_change(folder):
            if folder.name == 'call_qa':
                second.parent.mkdir(parents=True)
                second.write_text('external unapproved content', encoding='utf-8')
        self.controller.after_call = concurrent_change
        with self.assertRaisesRegex(ValueError, 'before durable apply plan'):
            S.maybe_review(self.controller)
        self.assertFalse(first.exists())
        self.assertEqual(second.read_text(), 'external unapproved content')

    def test_pause_after_proposal_defers_qa_and_edits_then_resumes_same_proposal(self):
        target = self.update()
        pause = self.root / S.RUNTIME / 'pause.request'
        def stop_after_proposal(folder):
            if folder.name == 'call_proposal':
                S.write(pause, {'requested': True})
        self.controller.after_call = stop_after_proposal
        self.assertEqual(S.maybe_review(self.controller)['status'], 'DEFERRED')
        self.assertEqual(len(self.controller.calls), 1)
        self.assertFalse(target.exists())
        pause.unlink()
        self.controller.after_call = None
        S.maybe_review(self.controller)
        self.assertEqual(len(self.controller.calls), 2)
        self.assertTrue(target.exists())

    def test_completed_raw_interruption_reconciles_accounting_and_recovers_output_once(self):
        self.controller.interrupt = True
        self.controller.omit_output = True
        with self.assertRaises(S.InterruptedSkillCall):
            S.maybe_review(self.controller)
        self.assertEqual(self.state['model_calls'], 528)
        self.assertEqual(self.state['active_invocation_s'], 0)
        self.assertTrue(S.reconcile_inflight(self.controller))
        self.assertFalse(S.reconcile_inflight(self.controller))
        self.assertEqual(self.state['active_invocation_s'], 1.25)
        S.maybe_review(self.controller)
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.state['skill_engineering']['next_review_at'], 550)

    def test_unreceipted_interruption_never_blindly_redispatches(self):
        self.controller.interrupt = True
        with self.assertRaises(S.InterruptedSkillCall):
            S.maybe_review(self.controller)
        call = self.controller.calls[0][2]
        (call / 'receipt.json').unlink()
        with self.assertRaises(OSError):
            S.reconcile_inflight(self.controller)
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(S.maybe_review(self.controller)['status'], 'DEFERRED')

    def test_forbidden_tool_attempt_is_charged_and_blocks_future_repeat(self):
        self.controller.events_extra = [{'type': 'item.completed', 'item': {'type': 'command_execution'}}]
        with self.assertRaisesRegex(ValueError, 'forbidden tools'):
            S.maybe_review(self.controller)
        self.assertEqual(self.state['model_calls'], 528)
        self.assertEqual(self.state['skill_engineering']['reviews']['540']['phase'], 'BLOCKED')

    def test_completed_native_https_fallback_is_not_a_tool_or_new_call(self):
        message=('stream disconnected before completion: No connection could be made because '
                 'the target machine actively refused it. (os error 10061)')
        prefix=[{'type':'thread.started','thread_id':'fixture'}, {'type':'turn.started'},
                {'type':'error','message':'Reconnecting... 2/5 ('+message+')'},
                {'type':'item.completed','item':{'type':'error','message':
                    'Falling back from WebSockets to HTTPS transport. '+message}}]
        final={'type':'item.completed','item':{'type':'agent_message','text':json.dumps(self.proposal)}}
        events=prefix+[final,{'type':'turn.completed'}]
        receipt={'exit_code':0,'process_reaped':True}
        raw='\n'.join(map(json.dumps,events))
        self.assertEqual(S.completed_native_fallback(raw,receipt,self.proposal,''),[prefix[0],prefix[1],final,events[-1]])
        for name,changed,terminal,output,stderr in (
            ('tool',events[:-1]+[{'type':'item.completed','item':{'type':'command_execution'}}]+events[-1:],receipt,self.proposal,''),
            ('unknown_error',prefix+[{'type':'error','message':'unknown'}]+events[-2:],receipt,self.proposal,''),
            ('failed',events[:-1]+[{'type':'turn.failed'}],receipt,self.proposal,''),
            ('not_reaped',events,{'exit_code':0},self.proposal,''),
            ('different_output',events,receipt,dict(self.proposal,summary='changed'),''),
            ('auth',events,receipt,self.proposal,'authentication failed'),
            ('late_error',events+prefix[2:3],receipt,self.proposal,''),
            ('malformed',events,receipt,self.proposal,'')):
            text='\n'.join(map(json.dumps,changed))+('\nnot json' if name=='malformed' else '')
            with self.subTest(name=name):self.assertIsNone(S.completed_native_fallback(text,terminal,output,stderr))

    def test_saved_notice_reconciliation_requires_exact_release_and_preserves_spent_call(self):
        message=('stream disconnected before completion: No connection could be made because '
                 'the target machine actively refused it. (os error 10061)')
        notice={'type':'item.completed','item':{'type':'error','message':
                'Falling back from WebSockets to HTTPS transport. '+message}}
        self.controller.events_extra=[notice]
        with patch.object(S,'completed_native_fallback',return_value=None),self.assertRaises(ValueError):
            S.maybe_review(self.controller)
        record=self.state['skill_engineering']['reviews']['540'];original=copy.deepcopy(record)
        folder=self.root/S.RUNTIME/'skill_engineering/540';call=folder/'call_proposal'
        events=[{'type':'thread.started','thread_id':'offline'}, {'type':'turn.started'},notice,
                {'type':'item.completed','item':{'type':'agent_message','text':json.dumps(self.proposal)}},
                {'type':'turn.completed'}]
        (call/'stdout.jsonl').write_text('\n'.join(map(json.dumps,events)),encoding='utf8')
        S.write(call/'receipt.json',{'exit_code':0,'process_reaped':True,'elapsed_s':1.25})
        proofdir=self.root/'research/campaigns/PAYOUT_V1/revisions/reconciliation';proofdir.mkdir(parents=True)
        def saved(name,value):
            path=proofdir/name;S.write(path,value);return S.pin(path)
        incident=saved('incident.json',{'incident_id':'fixture'})
        before={p:p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        proof=saved('reconcile.json',{'incident':incident,'record':original,'pins':[S.pin(p) for p in before]})
        record['completed_notice_reconciliation']=proof
        packet=saved('packet.json',{'skill_notice_reconciliation':proof,'source_hashes':{'installed/writing-plans/SKILL.md':S.digest(self.source)}})
        qa=saved('qa.json',{'verdict':'PASS'})
        decision=saved('lead.json',{'action':'RELEASE_REPAIRED_HOLD','worker_dispatch_ready':False})
        release=saved('release.json',{'incident':incident,'worker_dispatch_authorized':False,'counters_reset':False,
            'repair_packet':packet,'independent_qa':dict(qa,incident=incident,verdict='PASS'),
            'lead_decision':decision,'lead_call_proofs':[decision]})
        with patch('tools.quant_research_loop.safety_hold.delegated_authority',return_value={'approved':True}):
            with self.assertRaisesRegex(ValueError,'prior review blocked'):S.maybe_review(self.controller)
            self.state['delegated_incident_review_release']=release
            self.state['operator_safety_hold']={'active':False,'release':release}
            altered=call/'receipt.json';original_bytes=altered.read_bytes();altered.write_bytes(b'{}')
            with self.assertRaisesRegex(ValueError,'evidence identity mismatch'):S._reconcile_completed_notice(self.controller,record)
            altered.write_bytes(original_bytes)
            spent=self.state['model_calls'];seconds=self.state['active_invocation_s']
            result=S.maybe_review(self.controller)
            self.assertEqual(result['next_review_at'],550)
            self.assertEqual(self.state['model_calls'],spent)
            self.assertEqual(self.state['active_invocation_s'],seconds)
            self.assertEqual(len(self.controller.calls),1)
            self.assertEqual(record['failure'],original['failure'])
            self.assertEqual(record['calls'],original['calls'])
            for path,expected in before.items():self.assertEqual(path.read_bytes(),expected)
            self.assertIsNone(S.maybe_review(self.controller))

    def test_workflow_bundle_contains_real_pinned_context_and_explicit_coverage(self):
        qa = self.root / S.RUNTIME / 'qa/current/report.json'
        qa.parent.mkdir(parents=True)
        S.write(qa, {'summary': 'A repeated workflow failure with concrete findings.'})
        self.state['latest_qa_review'] = S.pin(qa)
        bundle = S._evidence(self.state, 540)
        self.assertIn('repeated workflow failure', bundle['workflow_evidence']['latest_qa']['content'])
        self.assertEqual(bundle['fixed_role_instruction']['content'], self.fixed.read_text(encoding='utf-8'))
        self.assertIn('no raw price', bundle['coverage'])
        self.assertIn('current_contract', bundle)


if __name__ == '__main__':
    unittest.main()
