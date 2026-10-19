"""Absolute-file entry for a short, exact Muse shell command."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
def main():
    if len(sys.argv) == 2:
        import json
        path=Path(sys.argv[1])
        job=json.loads(path.read_text(encoding='utf-8'))
        if job.get('mandate_revision') == 'R03_PAYOUT_FIRST_20260921':
            from tools.quant_research_loop.r03 import verified_job, calculate, ROOT
            from tools.quant_research_loop.gate_zero import write
            frozen,profile=verified_job(path)
            result=calculate(frozen,profile)
            out=ROOT/frozen['outputs'][0]
            if out.exists():
                if json.loads(out.read_text(encoding='utf-8')) != result:
                    raise ValueError('Existing result conflict; preserve both versions')
                print('EXISTING_RESULT_REUSED')
            else:
                write(out,result)
                print('REPLAY_COMPLETED '+frozen['job_id'])
            return
    from tools.quant_research_loop.replay_job import main as legacy_main
    legacy_main()

if __name__=='__main__':
    main()
