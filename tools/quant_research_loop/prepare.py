"""Freeze the first bounded saved-ledger job before new payout results."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import requests

from .gate_zero import ROOT, BASE, write
from .model_routing import MODEL, REASONING

CAMP = ROOT / 'research/campaigns/PAYOUT_V1'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    gate = json.loads((BASE / 'latest_gate.json').read_text(encoding='utf-8'))
    if gate['status'] != 'GATE_ZERO_PASSED':
        raise RuntimeError('Real roundtrip gate is required')
    dest = CAMP / 'runtime'
    dest.mkdir(exist_ok=True)
    if (dest / 'profile.json').exists():
        print('Existing frozen profile retained')
        return
    source_dir = dest / 'sources'
    source_dir.mkdir(exist_ok=True)
    old = json.loads((CAMP/'TOPSTEP_RULES_50K.json').read_text(encoding='utf-8'))
    sources = []
    for name, meta in old['retrieval']['snapshots'].items():
        response = requests.get(meta['url'], timeout=30)
        response.raise_for_status()
        path = source_dir / (name + '.html')
        path.write_bytes(response.content)
        sources.append(dict(name=name, url=meta['url'], sha256=digest(path),
                            path=path.relative_to(ROOT).as_posix(),
                            retrieved_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
    write(source_dir/'manifest.json', sources)
    rules = dict(version='PAYOUT_V1_STANDARD_50K_20260920', monthly=49, activation=149,
                 mll=2000, target=3000, consistency=.55, min_days=2,
                 combine_max_minis=5, scaling=None, dll=None,
                 winning_day=150, winning_days=5, payout_ratio=.5, payout_cap=2000,
                 trader_split=.9, min_payout=125, withdrawal_fee=30,
                 withdrawal_method='International Wire/SWIFT scenario; actual eligibility unknown',
                 request_timing='16:00 America/Chicago on eligible non-holiday weekday; unknown calendar blocks payout',
                 unknowns=['funded_scaling_table_not_verified', 'intraday_path_not_certified',
                           'account_specific_terms_unknown'],
                 source_manifest_sha256=digest(source_dir/'manifest.json'),
                 scenario='Standard pricing AND Standard XFA, no optional DLL, no legacy split',
                 source_effective_dates='Pricing July 20 2026; XFA August 5 2026; others retrieved current',
                 all_costs_known=False)
    rules['rules_hash'] = hashlib.sha256(json.dumps(rules,sort_keys=True).encode()).hexdigest()
    write(dest/'rules.json', rules)
    ledger = 'research/runs/B002_20260912T225535Z/cases/ORB_MNQ_fixed1__base__train/trades.csv'
    job = dict(job_id='PAYOUT_V1_J001', candidate_id='ORB_TESTMAX_FIXED1_MNQ_REPLAY',
               candidate_version='B002_base_preserved', operation='REPLAY_SAVED_LEDGER',
               rules='Preserved original ORB signal/structural stop/2R/time exit; fixed 1 MNQ proxy. No strategy modifications.',
               allowed_paths=[ledger, 'research/runs/B002_20260912T225535Z/contract.json',
                              'research/runs/B002_20260912T225535Z/verification.json'],
               input_hashes={ledger:digest(ROOT/ledger)}, data_partition='train',
               eval_interval=['2022-12-27','2024-10-04'],
               cost_profile_hash=digest(ROOT/'research/runs/B002_20260912T225535Z/contract.json'),
               account_profile_hash=digest(dest/'rules.json'),
               risk_policy='Preserve fixed 1 MNQ; no added daily stop; first eligible withdrawal; one attempt; $120 external cash scenario',
               outputs=['research/campaigns/PAYOUT_V1/runtime/jobs/PAYOUT_V1_J001/result.json'],
               verification=[['py','-3.12','-X','utf8','-m','tools.quant_research_loop.replay_job',
                              '--verify','research/campaigns/PAYOUT_V1/runtime/jobs/PAYOUT_V1_J001/job.json']])
    profile = dict(campaign_id='PAYOUT_V1', version='STANDARD50K_SCREEN_V1', live='DISABLED',
                   created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                   starts=['2023-01-03','2023-07-03','2024-01-02'], horizon_calendar_days=90,
                   starts_interpretation='Fixed dependent historical scenarios, not probability estimates',
                   external_cash_budget=120, monthly_target=.05, fixed_nominal_reference=50000,
                   target_status='Provisional user example; NOT reduced to any small payout',
                   concentration_cutoff=None, concentration_min_complete_quarters=4,
                   screening='All fixed starts positive known-cost net cash with payout and post-withdrawal survival; unknowns block qualification',
                   screening_status='Research default frozen before payout results, not user-confirmed purchase criterion',
                   spend_authorized=0, worker='opencode/muse-spark-1.3-contributor-free',
                   lead=MODEL, reasoning=REASONING, max_calls=80, max_active_seconds=21600,
                   max_families=6, families_per_batch=3, max_configs=18, max_per_family=3,
                   max_repairs=2, max_transport_retries=2, max_integration_fixes=3,
                   jobs=[job], rules_path='research/campaigns/PAYOUT_V1/runtime/rules.json',
                   historical_bootstrap_accounting='Prior bootstrap full call/time ledger unavailable; reserve 10 calls and 3600s conservatively, without increasing 80/21600 limits',
                   prior_reserved_calls=10, prior_reserved_seconds=3600)
    write(dest/'profile.json', profile)
    write(dest/'profile.sha256.json', {'sha256':digest(dest/'profile.json')})
    print('Profile and official snapshots frozen; no new payout results examined')


if __name__ == '__main__':
    main()
