"""Complete, bounded views of the saved NR7 pooled diagnostic; never reruns it.

Each view retains original indices. Coverage is the union of exact values, not a
sample or a synthesized result. All model calls use the existing QA dispatcher.
"""
import copy
import hashlib
import json
from pathlib import Path

from .gate_zero import ROOT, write
from .prepare import digest

VERSION = 1
MAX_CONTENT = 540000
REVIEW_CONTEXT_RESERVE = 20000
SECTIONS = ('records', 'root_cause_witnesses', 'executions', 'sources',
            'source_row_pool', 'event_pool')
V3_SECTIONS = ('finite_data_checks', 'synthetic_trades', 'unresolved_exposure',
               'synthetic_financial_checks')
SOURCE_METADATA_BATCH = 660
SOURCE_METADATA_FIELDS = {'metadata_scope_check_': ('changes', 'unexpected'),
                          'metadata_storage_check_': ('validated_storage', 'raw_unexpected')}
SOURCE_REPRESENTATION = 'selected_indexed_metadata_v1; whole-file coverage requires all views'


def sections(payload):
    # V3/V4 name their regression ledger after the compared predecessor.
    # Keep that exact field identity; historical V2/V3 selections stay unchanged.
    if 'finite_data_checks' not in payload:
        return SECTIONS
    ledgers = tuple('control_coverage.' + name for name in
        ('valid_v2_ledger_regressions', 'valid_v3_ledger_regressions')
        if name in payload.get('control_coverage', {}))
    return SECTIONS + V3_SECTIONS + ledgers


def section_value(payload, name):
    value = payload
    for key in name.split('.'):
        value = value[key]
    return value


def static_fields(payload):
    value = copy.deepcopy(payload)
    for name in sections(payload):
        parent = value
        keys = name.split('.')
        for key in keys[:-1]: parent = parent[key]
        del parent[keys[-1]]
    return value


class Number(str):
    """Retain the original JSON number spelling instead of parsing a float."""


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key in transport source')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Non-finite JSON constant: ' + value)
    return json.loads(text, parse_int=Number, parse_float=Number,
                      parse_constant=invalid, object_pairs_hook=pairs)


def emit(value):
    if isinstance(value, Number):
        return str(value)
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k, ensure_ascii=False) + ':' + emit(v)
                              for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(emit(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def pin(path):
    return {'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path)}


def checked(ref):
    path = (ROOT / ref['path']).resolve()
    if (not path.is_relative_to(ROOT.resolve()) or not path.is_file()
            or digest(path) != ref['sha256']):
        raise ValueError('QA transport pin missing or changed: ' + str(ref.get('path')))
    return path


def section_items(payload, section):
    value = section_value(payload, section)
    return list(value.items()) if isinstance(value, dict) else [(str(i), v) for i, v in enumerate(value)]


def reference_keys(value):
    """Only the saved result's declared execution/source references, not prose."""
    refs = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('execution_ref', 'source_ref', 'original_ref', 'corrected_ref',
                       'present_ref', 'missing_ref', 'original_present_ref', 'original_missing_ref',
                       'baseline_preprocessed_ref', 'quota_recovery_ref', 'positive_ref'):
                if not isinstance(item, str):
                    raise ValueError('Invalid named NR7 reference')
                refs.append(item)
            elif key in ('execution_refs', 'oracle_refs'):
                if not isinstance(item, list) or any(not isinstance(x, str) for x in item):
                    raise ValueError('Invalid NR7 reference list')
                refs.extend(item)
            else:
                refs.extend(reference_keys(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(reference_keys(item))
    return refs


def validate_references(payload):
    executions, sources = payload['executions'], payload['sources']
    for key in executions:
        source = key.removesuffix('|original')
        if source not in sources:
            raise ValueError('Dangling NR7 execution source: ' + key)
    for value in (payload['records'], payload['root_cause_witnesses'],
                  payload['synthetic_trades'], payload['unresolved_exposure'],
                  payload['synthetic_financial_checks'], payload['fixture_corrections'],
                  payload.get('finite_data_checks', []), payload.get('control_coverage', {})):
        for ref in reference_keys(value):
            if ref not in executions and ref not in sources:
                raise ValueError('Dangling NR7 named reference: ' + ref)
    for values, pool in ([(v['trace'], payload['event_pool']) for v in executions.values()]
                          + [(v, payload['source_row_pool']) for v in sources.values()]):
        if not isinstance(values, list):
            raise ValueError('NR7 reference sequence is not a list')
        for value in values:
            if not isinstance(value, Number) or not str(value).isdigit() or int(value) >= len(pool):
                raise ValueError('Dangling or non-integer NR7 pool reference')


def selected_content(result, selection, static=False):
    payload = result['payload']
    selected = {name: {key: value for key, value in section_items(payload, name)
                        if key in selection.get(name, set())} for name in sections(payload)}
    return emit({'root_fields': {k: v for k, v in result.items() if k != 'payload'} if static else {},
                 'payload_fields': static_fields(payload) if static else {},
                 'indexed_payload_sections': selected})


def closure(payload, records=(), executions=(), case=None):
    selected = {name: set() for name in sections(payload)}
    selected['records'].update(str(i) for i in records)
    keys = set(executions)
    for i in records:
        keys.update(key for key in payload['records'][i]['execution_refs']
                    if case is None or key.split('|')[1] == case)
    # Keep original/corrected and paired-prefix witnesses together.
    changed = True
    while changed:
        before = set(keys)
        for i, witness in enumerate(payload['root_cause_witnesses']):
            refs = set(reference_keys(witness))
            if refs & keys:
                selected['root_cause_witnesses'].add(str(i))
                keys.update(ref for ref in refs if ref in payload['executions'])
        # Follow a check from its tested execution to its exact comparators.
        # Shared positive controls do not reverse-select unrelated mutations.
        for check in payload.get('finite_data_checks', []):
            if check.get('execution_ref') in keys:
                keys.update(ref for ref in reference_keys(check) if ref in payload['executions'])
        changed = keys != before
    selected['executions'] = keys
    selected['sources'] = {key.removesuffix('|original') for key in keys}
    for key in keys:
        selected['event_pool'].update(str(i) for i in payload['executions'][key]['trace'])
    for key in selected['sources']:
        selected['source_row_pool'].update(str(i) for i in payload['sources'][key])
    for name in sections(payload)[len(SECTIONS):]:
        for i, value in section_items(payload, name):
            refs = set(reference_keys(value))
            if refs and (refs <= keys or value.get('execution_ref') in keys):
                if not refs <= keys:
                    raise ValueError('Cross-execution V3 check lacks a complete closure')
                selected[name].add(i)
    return selected


def merge_selection(left, right):
    return {name: left.get(name, set()) | right.get(name, set()) for name in left.keys() | right.keys()}


def file_content(sha, content, representation='complete_source_text'):
    return {'sha256': sha, 'content': content, 'truncated': False, 'representation': representation}


def _pointer_key(key):
    return key.replace('~', '~0').replace('/', '~1')


def _pointer_parent(value, pointer):
    keys=[k.replace('~1','/').replace('~0','~') for k in pointer.split('/')[1:]]
    for key in keys[:-1]:value=value[int(key) if isinstance(value,list) else key]
    return value,int(keys[-1]) if isinstance(value,list) else keys[-1]


def _market_rows(result):
    """Exact repeated rows and field-name templates, never numeric normalization.

    Only the saved result's named ledger/fixture/trace arrays are encoded.
    Every other field stays in the complete static document. No price file reads.
    """
    if ('synthetic_checks' in result['payload']
            and 'monetary_comparator' in result['payload'].get('semantic_diff', {})):
        # Asia adapter evidence: repeated comparator witnesses are shared only
        # when their complete lexical objects match. No adapter is executed.
        paths=['/payload/'+k for k in ('synthetic_checks','synthetic_trades','unresolved_exposure')]
        paths += ['/payload/market_input_contract/date_inventory',
                  '/payload/semantic_diff/monetary_comparator/witnesses']
    elif all(k in result['payload'] for k in ('evaluation_contract','calendar_inventory','business_screen')):
        paths=['/payload/'+k for k in ('trades','daily','marks','synthetic_checks',
                                      'calendar_inventory','unresolved_exposure')]
        paths += ['/payload/integration_checks/cases/'+str(i)+'/witnesses'
                  for i in range(len(result['payload']['integration_checks']['cases']))]
    elif 'integration_checks' in result['payload']:
        paths=['/payload/'+k for k in ('trades','daily','marks','money_reconciliation','correction_comparison')]
        paths.append('/payload/integration_checks/fixtures')
        traces=result['payload']['integration_checks']['market_execution_traces']
        paths += ['/payload/integration_checks/market_execution_traces/'+_pointer_key(k) for k in traces]
    else:
        # The original failed baseline remains complete comparison evidence.
        paths=['/payload/'+k for k in ('trades','daily_cash','risk_observations')]
    static=copy.deepcopy(result);templates={};template_ids={};rows={};row_ids={};arrays={};locations={}
    for pointer in paths:
        parent,key=_pointer_parent(static,pointer);items=parent[key]
        if not isinstance(items,list):raise ValueError('Market transport requires named arrays')
        array_id=str(len(arrays))
        ids=[]
        for index,item in enumerate(items):
            if not isinstance(item,dict):raise ValueError('Market transport requires complete object rows')
            token=emit(item)
            if token not in row_ids:
                ident=str(len(rows));row_ids[token]=ident;keys=tuple(item)
                if keys not in template_ids:
                    tid=str(len(templates));template_ids[keys]=tid;templates[tid]=list(keys)
                rows[ident]={'template':template_ids[keys],'values':list(item.values())}
                locations[ident]={}
            ident=row_ids[token];ids.append(int(ident))
            locations[ident].setdefault(array_id,[]).append(index)
        arrays[pointer]=ids;parent[key]=[]
    return static,templates,rows,arrays,locations


def build_market_views(full):
    """Use existing independent view/synthesis gates for the larger saved market job."""
    if full.get('deterministically_verified') is not True:
        raise ValueError('Complete market views require deterministic verification')
    if any(n not in full['files'] or full['files'][n].get('truncated')
           for n in full['required_complete_files']):
        raise ValueError('Incomplete market source collection before QA partitioning')
    if full.get('recovery_context', {}).get('truncated'):
        raise ValueError('Incomplete recovery accounting before QA partitioning')
    full['market_transport']='exact_rows_v1'
    originals=full['files'];name=next(n for n in full['required_complete_files']
        if full['job_id'] in n and n.endswith('/work/result.json'))
    static,templates,rows,arrays,locations=_market_rows(loads(originals[name]['content']))
    base={n:v for n,v in originals.items() if n.endswith('/generic_entry.py')
          or ('/'+full['job_id']+'/' in '/'+n and (n.endswith('/analysis.py') or n.endswith('/job.json')))}
    static_base=dict(base,**{n:v for n,v in originals.items() if n.endswith('.py')})
    static_base.update({n:v for n,v in originals.items()
                        if '/'+full['job_id']+'/worker_call_' in '/'+n
                        and n.endswith(('/request.json','/receipt.json','/native_policy_and_commands.json'))})
    # Actual saved invocation belongs beside the source whose execution path is
    # reviewed first. Deferring it to a later view creates an avoidable missing
    # context failure and cannot settle a real source/contract contradiction.
    text=context_text(full)
    static_base['controller_state/qa_context.json']=file_content(text_hash(text),text)
    array_paths={str(i):pointer for i,pointer in enumerate(arrays)}
    views=[];limit=MAX_CONTENT-REVIEW_CONTEXT_RESERVE
    def add(part,scope):
        files=dict(static_base if part['kind']=='static' else base)
        files[name]=file_content(originals[name]['sha256'],emit(part),'exact_market_rows_v1')
        if size(files)>limit:raise ValueError('Complete market view exceeds bound')
        views.append({'files':files,'selection':None,'static':part['kind']=='static','scope':scope})
    add({'kind':'static','value':static,'templates':templates},
        'Review full current source and pinned source dependencies plus every static result field, records and '
        'reporting claims and actual saved invocation in qa_context. Identify any source/contract '
        'conflict from that exact invocation; do not infer compliance from successful completion. '
        'Named arrays are explicitly empty placeholders only in this static view; their '
        'complete ordered reference lists and all exact row values are mandatory subsequent views. '
        'Do not treat placeholders as original empty ledgers or inherit V4 market acceptance. Check V4 '
        'correspondence, adapter/loader chronology, risk/cost/permission rules and derive requirements for '
        'the remaining row reviews. Only final independent synthesis can accept the full market job.')
    reference_scope=('Review complete ordered original-array index to pooled-row-ID mappings. JSON pointer '
        'escaping is ~1 slash and ~0 tilde. Each ID denotes exactly the row reviewed in mandatory row views; '
        'duplicates retain every original occurrence, order and path. This mapping-only view proves no '
        'missing row semantics. Row views include complete field-name templates, values and inverse locations. '
        'All mappings and all values must pass independently and reconstruct the original numeric lexemes '
        'before final synthesis. No research rerun, pooled trade-count inference or worker grant.')
    room=limit-size(base)-1000;batch={};used=0
    for pointer,ids in arrays.items():
        amount=len(emit({pointer:ids}))+1
        if batch and used+amount>room:add({'kind':'references','arrays':batch},reference_scope);batch={};used=0
        batch[pointer]=ids;used+=amount
    if batch:add({'kind':'references','arrays':batch},reference_scope)
    row_scope=('Review all supplied exact saved rows, not samples. A row has template ID and values: zip '
        'the supplied ordered template field names with its equally long values to recover the complete '
        'original object. Nested values and number spelling are unchanged. Inverse locations use the '
        'supplied array_paths dictionary to give every JSON array pointer and zero-based index where '
        'this identical row occurs; full forward '
        'mappings are reviewed separately. Check applicable fixture expected/observed evidence, ledger '
        'money/risk, daily/marks/envelope fields and trace chronology against the full supplied source. '
        'Duplicate trace rows across dependent starts remain dependent; no rows or failures are discarded. '
        'Cross-view ordering and whole-job reconciliation remain mandatory in final synthesis; any material '
        'gap blocks acceptance. No market/payout qualification follows from a partial review.')
    batch={};loc={};used=0
    # Include the complete small template dictionary in each row view.
    room=limit-size(base)-len(emit(templates))-len(emit(array_paths))-1500
    for ident,row in rows.items():
        amount=len(emit({ident:row}))+len(emit({ident:locations[ident]}))+2
        if batch and used+amount>room:
            add({'kind':'rows','templates':templates,'array_paths':array_paths,'rows':batch,'locations':loc},row_scope);batch={};loc={};used=0
        batch[ident]=row;loc[ident]=locations[ident];used+=amount
    if batch:add({'kind':'rows','templates':templates,'array_paths':array_paths,'rows':batch,'locations':loc},row_scope)
    supports=full.get('supporting_market_results',[])
    for support in supports:
        child=_market_support(full,support)
        children,_=build_market_views(child)
        for view in children:
            view['supporting_result']=support
            view['scope']=('Historical failed baseline comparison evidence only; no retroactive acceptance. '+view['scope'])
        views.extend(children)
    remaining={n:v for n,v in originals.items() if n!=name and n not in static_base and n not in supports}
    if full.get('recovery_context'):
        x=full['recovery_context'];remaining['controller_state/recovery_context.json']=file_content(x['sha256'],x['content'])
    text=context_text(full);remaining['controller_state/qa_context.json']=file_content(text_hash(text),text)
    files=dict(base)
    for n,item in remaining.items():
        if size(dict(files,**{n:item}))>limit:
            views.append({'files':files,'selection':None,'static':False,'scope':
                'Review complete supporting contract, native/failure/permission receipts and current recovery accounting supplied here. Historical accepted maintenance is context only; no acceptance inheritance. Other material duties are assigned to complete result views and final synthesis.'})
            files=dict(base)
        files[n]=item
        if size(files)>limit:raise ValueError('Market supporting file exceeds bounded view')
    if len(files)>len(base):views.append({'files':files,'selection':None,'static':False,'scope':
        'Review complete supporting evidence and current recovery accounting. Preserve historical limitations; final synthesis must reconcile every material view before any Lead grant.'})
    if '/payload/market_input_contract/date_inventory' in arrays:
        for view in views:
            view['scope'] += (' This is synthetic Asia adapter evidence, not a market evaluation. '
                'Review source correspondence and corrective delta, whole-payload serialization, '
                'all three cost/timestamp cases, all synthetic checks/trades/unresolved exposures and '
                'the complete date inventory. Verify independent expected operands, baseline and '
                'neutral acceptance, and every positive/negative one-cent mutation against the actual '
                'comparator source. Reported PASS labels are assertions to audit, not inherited QA. '
                'Market price admission remains closed; NR7 remains PARK. References to V4 market '
                'correspondence in the shared transport scope do not apply to this synthetic job.')
    elif '/payload/calendar_inventory' in arrays:
        for view in views:
            view['scope'] += (' This is the saved Asia fixed-rule baseline on consumed 2026 development data. '
                'Review its exact current contract, all three dependent cost/timestamp cases, synthetic checks, '
                'calendar, ledgers and reporting fields. References to V4 correspondence in the shared scope '
                'do not apply. No market rerun, NR7 repair, worker grant or inherited semantic acceptance.')
    return views,validate_market_coverage(full,views)


def _market_support(full,name):
    if name not in full['required_complete_files']:raise ValueError('Unknown required market dependency')
    jid=Path(name).parent.parent.name
    child=dict(full,job_id=jid,supporting_market_results=[])
    child['files']={n:v for n,v in full['files'].items()
        if n==name or n.endswith('.py') or n.endswith('/'+jid+'/job.json')}
    child['required_complete_files']=list(child['files'])
    return child


def validate_market_coverage(full,views):
    originals=full['files'];required=full['required_complete_files']
    name=next(n for n in required if full['job_id'] in n and n.endswith('/work/result.json'))
    source=loads(originals[name]['content']);static,templates,rows,arrays,locations=_market_rows(source)
    seen_static=None;seen_rows={};seen_arrays={};whole=set();support_coverage={}
    supports=full.get('supporting_market_results',[])
    for support in supports:
        children=[dict(v,supporting_result=None) for v in views if v.get('supporting_result')==support]
        support_coverage[support]=validate_market_coverage(_market_support(full,support),children);whole.add(support)
    for view in views:
        if view.get('supporting_result'):
            if view['supporting_result'] not in supports:raise ValueError('Unknown market support view')
            continue
        if size(view['files'])>MAX_CONTENT or any(x.get('truncated') for x in view['files'].values()):
            raise ValueError('Oversized or truncated market view')
        for path,item in view['files'].items():
            if path==name:
                if item['sha256']!=originals[name]['sha256']:raise ValueError('Market result hash mismatch')
                value=loads(item['content']);kind=value.get('kind')
                if kind=='static':
                    if seen_static is not None or emit(value)!=emit({'kind':'static','value':static,'templates':templates}):
                        raise ValueError('Market static content mismatch or duplicate')
                    seen_static=value['value']
                elif kind=='references':
                    if set(value)!= {'kind','arrays'}:raise ValueError('Unexpected market reference fields')
                    for pointer,ids in value['arrays'].items():
                        if pointer in seen_arrays or pointer not in arrays or emit(ids)!=emit(arrays[pointer]):
                            raise ValueError('Market array range/order/reference mismatch')
                        seen_arrays[pointer]=ids
                elif kind=='rows':
                    if (set(value)!={'kind','templates','array_paths','rows','locations'} or value['templates']!=templates
                            or value['array_paths']!={str(i):p for i,p in enumerate(arrays)}
                            or set(value['rows'])!=set(value['locations'])):raise ValueError('Market template/location mismatch')
                    for ident,row in value['rows'].items():
                        if (ident in seen_rows or ident not in rows or emit(row)!=emit(rows[ident])
                                or emit(value['locations'][ident])!=emit(locations[ident])):
                            raise ValueError('Market row value/number/location mismatch')
                        seen_rows[ident]=row
                else:raise ValueError('Unknown market transport part')
            elif path in originals:
                if item['sha256']!=originals[path]['sha256'] or item['content']!=originals[path]['content']:
                    raise ValueError('Market supporting evidence mismatch')
                whole.add(path)
            elif path=='controller_state/recovery_context.json':
                if item['sha256']!=full['recovery_context']['sha256'] or item['content']!=full['recovery_context']['content']:
                    raise ValueError('Market recovery accounting mismatch')
                whole.add(path)
            elif path=='controller_state/qa_context.json':
                if item['content']!=context_text(full) or item['sha256']!=text_hash(context_text(full)):
                    raise ValueError('Market controller context mismatch')
                whole.add(path)
            else:raise ValueError('Unknown market evidence')
    if (seen_static is None or set(seen_rows)!=set(rows) or set(seen_arrays)!=set(arrays)
            or set(required)-{name}-whole or 'controller_state/qa_context.json' not in whole
            or (full.get('recovery_context') and 'controller_state/recovery_context.json' not in whole)):
        raise ValueError('Incomplete market evidence coverage')
    rebuilt=copy.deepcopy(seen_static)
    for pointer,ids in seen_arrays.items():
        parent,key=_pointer_parent(rebuilt,pointer);decoded=[]
        for ident in ids:
            row=seen_rows[str(ident)];fields=templates[row['template']]
            if len(fields)!=len(row['values']):raise ValueError('Market row arity mismatch')
            decoded.append(dict(zip(fields,row['values'])))
        parent[key]=decoded
    if emit(rebuilt)!=emit(source):raise ValueError('Market lexical reconstruction mismatch')
    coverage={'complete':True,'format':'exact_rows_v1','result_sha256':originals[name]['sha256'],
        'lexical_value_sha256':text_hash(emit(source)),'reconstructed_sha256':text_hash(emit(rebuilt)),
        'array_counts':{p:len(a) for p,a in arrays.items()},'records':len(source['payload']['records']),
        'unique_rows':len(rows),'duplicate_rows_referenced':sum(map(len,arrays.values()))-len(rows),
        'templates':len(templates),'views':len(views),'required_files':{n:originals[n]['sha256'] for n in required},
        'recovery_context_sha256':full.get('recovery_context',{}).get('sha256'),
        'max_content_chars':max(size(v['files']) for v in views),
        'claim_limit':'Complete source pointers, order, multiplicity and numeric lexemes reconstructed. Only identical objects share IDs. This is transport coverage, not semantic acceptance.'}
    if support_coverage:coverage['supporting_results']=support_coverage
    return coverage


def size(files):
    return sum(len(item['content']) for item in files.values())


def context_text(full):
    return json.dumps({k:v for k,v in full.items() if k not in
        ('files','review_key','required_complete_files','recovery_context')},
        ensure_ascii=False, separators=(',', ':'))


def supporting_results(full):
    """Exact historical pooled results need their own complete views too."""
    found = {}
    for name in full['required_complete_files']:
        if not name.endswith('/work/result.json') or full['job_id'] in name:
            continue
        value = loads(full['files'][name]['content'])
        if isinstance(value, dict) and all(k in value.get('payload', {}) for k in SECTIONS):
            prefix = name.removesuffix('/work/result.json')
            selected = {name, prefix+'/job.json', prefix+'/work/analysis.py'}
            current_source = next(p for p in full['required_complete_files']
                if p.endswith('/work/analysis.py') and full['job_id'] in p)
            selected.add(current_source)
            job = json.loads(full['files'][prefix+'/job.json']['content'])
            selected.update(i['path'] for i in job.get('inputs', [])
                if i['path'] in full['files'] and not i['path'].endswith('/work/result.json'))
            # Frozen specification/audit results are small, non-pooled context.
            for i in job.get('inputs', []):
                if i.get('alias') in ('specification', 'audit_result') and i['path'] in full['files']:
                    selected.add(i['path'])
            files = {p: full['files'][p] for p in full['files'] if p in selected}
            if selected-set(files): raise ValueError('Historical pooled result lacks its source/contract')
            found[name] = {'job_id':prefix.rsplit('/',1)[-1], 'files':files,
                'required_complete_files':list(files), 'deterministically_verified':True,
                'scope':'Historical failed result, supplied only for exact comparison; no new trial or inherited PASS.',
                'historical_result':True, 'comparison_source':current_source}
    return found


def source_metadata_fields(path):
    return next((fields for prefix, fields in SOURCE_METADATA_FIELDS.items()
                 if Path(path).name.startswith(prefix)), None)


def build_source_views(full):
    """Keep source/result whole; partition only saved permissions inventory rows."""
    originals=full['files'];required=full['required_complete_files']
    full['source_transport']='exact_metadata_v1'
    metadata={n:loads(originals[n]['content']) for n in required if source_metadata_fields(n)}
    if not metadata:raise ValueError('Source transport requires original metadata inventories')
    limit=MAX_CONTENT-REVIEW_CONTEXT_RESERVE
    files={n:originals[n] for n in required if n not in metadata}
    context=context_text(full)
    files['controller_state/qa_context.json']=file_content(text_hash(context),context)
    if full.get('recovery_context'):
        x=full['recovery_context'];files['controller_state/recovery_context.json']=file_content(x['sha256'],x['content'])
    views=[]
    def add(files,scope,static=False):
        if size(files)>limit:raise ValueError('Source QA view exceeds bounded content')
        views.append({'files':files,'scope':scope,'static':static,'selection':None})
    add(files,'Review the complete source-only saved job, implementation, original result, primary source bodies, '
        'fresh original dispatch grant, provenance disposition, actual execution/permission receipts and recovery '
        'context. Preserve page markers, incomplete disclosures, no-ML/RL and UNKNOWN claims. Large raw metadata '
        'inventories are reviewed in subsequent complete indexed views; this view alone cannot accept the job. '
        'Source clues, reported author profits and completed source work provide no market/payout qualification.',True)
    job_name=next(n for n in required if n.endswith('/job.json') and full['job_id'] in n)
    maximum=max(len(v[field]) for n,v in metadata.items() for field in source_metadata_fields(n))
    for start in range(0,max(1,maximum),SOURCE_METADATA_BATCH):
        selected={job_name:originals[job_name]}
        for name,value in metadata.items():
            fields=source_metadata_fields(name);indexed={}
            for field in fields:
                items=value[field]
                if not isinstance(items,(dict,list)):raise ValueError('Metadata field requires object or array')
                entries=([[i,k,v] for i,(k,v) in enumerate(items.items()) if start<=i<start+SOURCE_METADATA_BATCH]
                         if isinstance(items,dict) else
                         [[i,v] for i,v in enumerate(items) if start<=i<start+SOURCE_METADATA_BATCH])
                indexed[field]={'type':'object' if isinstance(items,dict) else 'array',
                                'count':len(items),'entries':entries}
            part={'root_field_order':list(value),'static_fields':{k:v for k,v in value.items() if k not in fields},
                  'indexed_fields':indexed}
            selected[name]=file_content(originals[name]['sha256'],emit(part),SOURCE_REPRESENTATION)
        add(selected,'Review all supplied original-index entries of the paired raw scope and storage inventories. '
            'Entries are [index,key,value] for objects and [index,value] for arrays; root_field_order and static_fields '
            'preserve every other original field. Empty selected entries are placeholders for indices in other views, '
            'not original empty inventories. Compare each scope path and storage classification, receipts/hashes and '
            'UNKNOWN historical actor/byte attribution. Union coverage must reconstruct exact ordered values and '
            'number spelling before final synthesis; this partial permissions review grants no worker or research PASS.')
    return views,validate_source_coverage(full,views)


def validate_source_coverage(full,views):
    originals=full['files'];required=full['required_complete_files']
    if full.get('deterministically_verified') is not True or any(
            n not in originals or originals[n].get('truncated') for n in required):
        raise ValueError('Incomplete verified source collection')
    name=next(n for n in required if n.endswith('/work/result.json') and full['job_id'] in n)
    result=loads(originals[name]['content']);payload=result.get('payload',{})
    if (result.get('job_class')!='DISCOVERY' or str(payload.get('market_rows_read'))!='0'
            or str(payload.get('trade_count'))!='0' or payload.get('trades')!=[]):
        raise ValueError('Source transport cannot encode a market evaluation')
    metadata={n:loads(originals[n]['content']) for n in required if source_metadata_fields(n)}
    observed={n:{field:[] for field in source_metadata_fields(n)} for n in metadata};whole=set();seen_metadata=set()
    for view in views:
        if size(view['files'])>MAX_CONTENT or any(v.get('truncated') for v in view['files'].values()):
            raise ValueError('Oversized or truncated source QA view')
        for path,item in view['files'].items():
            if path in metadata:
                seen_metadata.add(path)
                original=originals[path];value=metadata[path];fields=source_metadata_fields(path)
                if item['sha256']!=original['sha256'] or item.get('representation')!=SOURCE_REPRESENTATION:
                    raise ValueError('Metadata original identity or representation changed')
                part=loads(item['content'])
                static={k:v for k,v in value.items() if k not in fields}
                if (set(part)!= {'root_field_order','static_fields','indexed_fields'}
                        or part['root_field_order']!=list(value) or emit(part['static_fields'])!=emit(static)
                        or list(part['indexed_fields'])!=list(fields)):
                    raise ValueError('Metadata static fields, order or section changed')
                for field in fields:
                    source=value[field];section=part['indexed_fields'][field];entries=section['entries']
                    is_object=isinstance(source,dict);expected=list(source.items()) if is_object else source
                    if (set(section)!= {'type','count','entries'} or section['type']!=('object' if is_object else 'array')
                            or str(section['count'])!=str(len(source)) or not isinstance(entries,list)):
                        raise ValueError('Metadata section identity or count changed')
                    for entry in entries:
                        index=len(observed[path][field])
                        if (not isinstance(entry,list) or len(entry)!=(3 if is_object else 2)
                                or str(entry[0])!=str(index) or index>=len(source)):
                            raise ValueError('Metadata index gap, duplicate or order changed')
                        target=[index,*expected[index]] if is_object else [index,expected[index]]
                        if emit(entry)!=emit(target):raise ValueError('Metadata key/value/numeric lexeme changed')
                        observed[path][field].append(entry)
            elif path in originals:
                if item!=originals[path]:raise ValueError('Complete source/supporting evidence changed')
                whole.add(path)
            elif path=='controller_state/qa_context.json':
                text=context_text(full)
                if item!=file_content(text_hash(text),text):raise ValueError('Source controller context changed')
                whole.add(path)
            elif path=='controller_state/recovery_context.json':
                x=full.get('recovery_context',{})
                if item!=file_content(x.get('sha256'),x.get('content')):raise ValueError('Source recovery context changed')
                whole.add(path)
            else:raise ValueError('Unknown source view file')
    if (set(metadata)-seen_metadata or set(required)-set(metadata)-whole or 'controller_state/qa_context.json' not in whole
            or full.get('recovery_context') and 'controller_state/recovery_context.json' not in whole):
        raise ValueError('Missing complete source/supporting/context coverage')
    if not views or name not in views[0]['files'] or views[0]['files'][name]!=originals[name]:
        raise ValueError('First source review lacks complete original result')
    manifests={}
    for path,value in metadata.items():
        rebuilt={}
        for key in value:
            if key not in observed[path]:rebuilt[key]=value[key];continue
            entries=observed[path][key]
            if len(entries)!=len(value[key]):raise ValueError('Missing metadata field/index coverage')
            rebuilt[key]=({entry[1]:entry[2] for entry in entries} if isinstance(value[key],dict)
                          else [entry[1] for entry in entries])
        if emit(rebuilt)!=emit(value):raise ValueError('Source metadata lexical reconstruction failed')
        manifests[path]={'original_sha256':originals[path]['sha256'],
            'section_counts':{k:len(v) for k,v in observed[path].items()},
            'lexical_value_sha256':text_hash(emit(value)),'reconstructed_sha256':text_hash(emit(rebuilt))}
    return {'complete':True,'result_sha256':originals[name]['sha256'],'metadata':manifests,
        'required_files':{n:originals[n]['sha256'] for n in required},'views':len(views),
        'max_content_chars':max(size(v['files']) for v in views),
        'claim_limit':'Exact original ordered values and numeric lexemes, not byte-identical whitespace. '
        'Metadata-only source transport; no historical scope PASS, research acceptance or worker authority.'}


def build_views(full):
    """Pure saved-text transformation. Every original required item stays covered."""
    if full.get('deterministically_verified') is not True:
        raise ValueError('Complete views require the existing deterministic verification')
    originals = full['files']
    required = full['required_complete_files']
    if any(name not in originals or originals[name].get('truncated') for name in required):
        raise ValueError('Incomplete source collection before QA partitioning')
    if full.get('recovery_context', {}).get('truncated'):
        raise ValueError('Incomplete recovery accounting before QA partitioning')
    result_name = next(name for name in required if name.endswith('/work/result.json')
                       and full['job_id'] in name)
    result_file = originals[result_name]
    result = loads(result_file['content']); payload = result['payload']
    if (result.get('job_class')=='DISCOVERY' and str(payload.get('market_rows_read'))=='0'
            and str(payload.get('trade_count'))=='0' and payload.get('trades')==[]):
        return build_source_views(full)
    if any(name not in payload for name in SECTIONS):
        raise ValueError('This transport requires the saved NR7 pooled-evidence schema')
    validate_references(payload)
    job_name = next(name for name in required if name.endswith('/job.json') and full['job_id'] in name)
    common = {name: originals[name] for name in required
              if name == job_name or name.endswith('.py') and '/revisions/' not in name
              and (name.endswith('/work/analysis.py') or name.endswith('/generic_entry.py'))}
    execution_common = {name:item for name,item in common.items()
                        if not name.endswith('/work/analysis.py') or full['job_id'] in name
                        or name == full.get('comparison_source')}
    job = json.loads(originals[job_name]['content'])
    static_common = dict(common)
    for item in job.get('inputs', []):
        if item.get('alias') in ('effective_profile','baseline_contract','specification','audit_result',
                                'production_parity_qa','execution_qa') and item['path'] in originals:
            static_common[item['path']] = originals[item['path']]
    views = []

    def add(files, scope, selection=None, static=False):
        if size(files) > MAX_CONTENT:
            raise ValueError('Complete QA view cannot fit supported content limit')
        views.append({'files': files, 'scope': scope, 'selection': selection, 'static': static})

    def research_files(selection, static=False):
        content = selected_content(result, selection, static)
        return dict(static_common if static else execution_common, **{result_name: file_content(result_file['sha256'], content,
            'complete_selected_fields; original-index maps; full-file coverage requires all views')})

    names = sections(payload)
    empty = {name: set() for name in names}
    add(research_files(empty, True),
        'Review all static result fields, frozen rules and claim limits against complete current/baseline source. '
        'Per-execution checks and money fields, when indexed, are reviewed with complete traces in separate views.', empty, True)
    # Keep shared production source current in every execution view; the full
    # baseline source is independently reviewed once in the first static view.
    weights = {name:{key:len(json.dumps(key,ensure_ascii=False))+len(emit(value))+2
                     for key,value in section_items(payload,name)} for name in names}
    empty_cost = size(execution_common)+len(selected_content(result,empty))
    def cost(selection):
        return empty_cost + sum(sum(weights[name][k] for k in keys)-bool(keys)
                                for name,keys in selection.items())
    groups = []
    for i,record in enumerate(payload['records']):
        group=closure(payload,records=[i])
        if cost(group)<=MAX_CONTENT-REVIEW_CONTEXT_RESERVE:
            groups.append(group)
        else:
            # Multi-date DST evidence: keep all cost cases of one fixture together.
            fixtures=sorted({key.split('|')[0] for key in record['execution_refs']})
            for fixture in fixtures:
                part=closure(payload,executions=[key for key in record['execution_refs'] if key.split('|')[0]==fixture])
                part['records'].add(str(i));groups.append(part)
    covered_exec=set().union(*(g['executions'] for g in groups))
    groups.extend(closure(payload,executions=[key]) for key in payload['executions'] if key not in covered_exec)
    # Pack overlapping controls together so shared pools occur once per view.
    # Required current source is repeated only as context for separate reviewers.
    while groups:
        index=max(range(len(groups)),key=lambda i:cost(groups[i]))
        selected=groups.pop(index)
        while groups:
            fitting=[(cost(merge_selection(selected,g)),i) for i,g in enumerate(groups)
                     if cost(merge_selection(selected,g))<=MAX_CONTENT-REVIEW_CONTEXT_RESERVE]
            if not fitting:break
            _,index=min(fitting)
            selected=merge_selection(selected,groups.pop(index))
        add(research_files(selected), 'Review only the complete executions/cost cases present here and their '
            'control comparisons/paired witnesses, with all referenced rows/events and current production source. '
            'Other executions mentioned by multi-date control records are covered in separate views. '
            'The independently accepted baseline/source-correspondence review is supplied as context; '
            'do not reclassify this selected view as whole-job acceptance.', selected)
    covered = {name: set().union(*(v['selection'][name] for v in views if v['selection'] is not None)) for name in names}
    # Preserve every otherwise-unreferenced pooled item too; it is not a new trial.
    extra = {name: {k for k, _ in section_items(payload, name)} - covered[name] for name in names}
    for key in extra['sources']:
        extra['source_row_pool'].update(str(i) for i in payload['sources'][key])
    if any(extra.values()):
        add(research_files(extra), 'Review all remaining saved pooled entries and witnesses. These are '
            'complete unreferenced entries, not samples or new research evidence.', extra)

    already = set(static_common) | {result_name}
    supporting = {name: originals[name] for name in required if name not in already}
    for name, historical in supporting_results(full).items():
        historical_views, _ = build_views(historical)
        for view in historical_views:
            # Scope remains the current job's QA, while original file names,
            # source indices and historical failure labels remain explicit.
            view['supporting_result'] = name
            view['scope'] = 'Historical failed result comparison only; no inherited acceptance. ' + view['scope']
        views.extend(historical_views)
        supporting.pop(name, None)
    context = context_text(full)
    supporting['controller_state/qa_context.json'] = file_content(text_hash(context), context,
        'complete_controller_context; includes native trace/actions and disposition; no new authority')
    # The raw scope delta and its storage disposition are one permissions view.
    pair = {name: item for name, item in supporting.items()
            if '/metadata_scope_check_' in name or '/metadata_storage_check_' in name}
    if pair:
        add(dict({job_name: originals[job_name]}, **pair),
            'Review the complete raw metadata scope delta and its complete storage classification together. '
            'Preserve UNKNOWN historical attribution and require exact matching receipt/hash identities.')
        for name in pair: supporting.pop(name)
    batch = {job_name: originals[job_name]}
    for name, item in supporting.items():
        if size(dict(batch, **{name: item})) > MAX_CONTENT-REVIEW_CONTEXT_RESERVE and len(batch) > 1:
            add(batch, 'Review these complete supporting contracts, native/verification receipts and maintenance '
                'proofs within their stated scope. Other views separately cover the job implementation/result.')
            batch = {job_name: originals[job_name]}
        batch[name] = item
    accounting = full.get('recovery_context')
    if accounting:
        name = 'controller_state/recovery_context.json'
        item = file_content(accounting['sha256'], accounting['content'], 'complete_current_recovery_context')
        if size(dict(batch, **{name: item})) > MAX_CONTENT-REVIEW_CONTEXT_RESERVE:
            add(batch, 'Review complete supporting evidence. No research acceptance or worker grant.')
            batch = {job_name: originals[job_name]}
        batch[name] = item
    if len(batch) > 1: add(batch, 'Review complete supporting evidence and current recovery accounting; no new permission.')
    coverage = validate_coverage(full, views)
    # Sets are deterministic selection metadata; content already preserves source order.
    for view in views:
        if view['selection'] is not None:
            view['selection'] = {k: sorted(v) for k, v in view['selection'].items()}
    return views, coverage


def validate_coverage(full, views):
    if full.get('source_transport') == 'exact_metadata_v1':
        return validate_source_coverage(full, views)
    if full.get('market_transport') == 'exact_rows_v1':
        return validate_market_coverage(full, views)
    originals = full['files']; required = full['required_complete_files']
    name = next(p for p in required if p.endswith('/work/result.json') and full['job_id'] in p)
    source = loads(originals[name]['content']); payload = source['payload']
    validate_references(payload)
    names = sections(payload)
    observed = {section: {} for section in names}; static = None; whole = set(); control_refs = set()
    histories = supporting_results(full)
    history_coverage = {}
    for history_name, historical in histories.items():
        children = [dict(v, supporting_result=None) for v in views if v.get('supporting_result')==history_name]
        history_coverage[history_name] = validate_coverage(historical, children)
        whole.add(history_name)
    for view in views:
        if size(view['files']) > MAX_CONTENT or any(v.get('truncated') for v in view['files'].values()):
            raise ValueError('Oversized or truncated complete QA view')
        comparison = full.get('comparison_source')
        if comparison and name in view['files'] and view['files'].get(comparison) != originals[comparison]:
            raise ValueError('Historical comparison lacks current source')
        if view.get('supporting_result'):
            if view['supporting_result'] not in histories: raise ValueError('Unknown supporting result view')
            continue
        for path, item in view['files'].items():
            if path == name:
                if item['sha256'] != originals[path]['sha256']:
                    raise ValueError('QA result identity mismatch')
                value = loads(item['content'])
                if view['static']:
                    expected_root = {k:v for k,v in source.items() if k != 'payload'}
                    expected_payload = static_fields(payload)
                    if emit(value['root_fields']) != emit(expected_root) or emit(value['payload_fields']) != emit(expected_payload):
                        raise ValueError('QA static lexical content mismatch')
                    static = value
                elif value['root_fields'] or value['payload_fields']:
                    raise ValueError('Unexpected static fields in selected view')
                for section in names:
                    actual = value['indexed_payload_sections'][section]
                    if set(actual) != set(view['selection'][section]):
                        raise ValueError('QA selection/coverage mismatch')
                    expected = dict(section_items(payload, section))
                    for key, item_value in actual.items():
                        if key not in expected or emit(item_value) != emit(expected[key]):
                            raise ValueError('QA selected value or numeric lexeme changed')
                        observed[section][key] = item_value
                selected = value['indexed_payload_sections']
                for rows in selected['sources'].values():
                    if any(str(i) not in selected['source_row_pool'] for i in rows):
                        raise ValueError('Dangling source pool inside complete QA view')
                for key, execution in selected['executions'].items():
                    source_key = key.removesuffix('|original')
                    if (source_key not in selected['sources']
                            or any(str(i) not in selected['event_pool'] for i in execution['trace'])
                            or any(str(i) not in selected['source_row_pool'] for i in selected['sources'][source_key])):
                        raise ValueError('Dangling reference inside complete execution view')
                for i, record in selected['records'].items():
                    for ref in record['execution_refs']:
                        if ref in selected['executions']: control_refs.add((i, ref))
                for witness in selected['root_cause_witnesses'].values():
                    if any(ref not in selected['executions'] and ref not in selected['sources']
                           for ref in reference_keys(witness)):
                        raise ValueError('Dangling paired witness in complete QA view')
                for section in names[len(SECTIONS):]:
                    for check in selected[section].values():
                        if any(ref not in selected['executions'] and ref not in selected['sources']
                               for ref in reference_keys(check)):
                            raise ValueError('Dangling per-execution check in complete QA view')
            elif path in originals:
                if item['sha256'] != originals[path]['sha256'] or item['content'] != originals[path]['content']:
                    raise ValueError('QA supporting file changed or incomplete')
                whole.add(path)
            elif path == 'controller_state/recovery_context.json':
                if item['content'] != full['recovery_context']['content'] or item['sha256'] != full['recovery_context']['sha256']:
                    raise ValueError('QA recovery accounting changed')
                whole.add(path)
            elif path == 'controller_state/qa_context.json':
                context = context_text(full)
                if item['content'] != context or item['sha256'] != text_hash(context):
                    raise ValueError('QA controller context changed')
                whole.add(path)
            else:
                raise ValueError('Unknown QA view source')
    if static is None or set(required) - {name} - whole:
        raise ValueError('Missing required QA file coverage')
    if full.get('recovery_context') and 'controller_state/recovery_context.json' not in whole:
        raise ValueError('Missing QA recovery coverage')
    if 'controller_state/qa_context.json' not in whole:
        raise ValueError('Missing QA controller context coverage')
    if control_refs != {(str(i), ref) for i,r in enumerate(payload['records']) for ref in r['execution_refs']}:
        raise ValueError('Missing control/execution QA coverage')
    reconstructed = copy.deepcopy(source)
    for section in names:
        expected = section_items(payload, section)
        if set(observed[section]) != {k for k, _ in expected}:
            raise ValueError('Missing pooled QA coverage: ' + section)
        parent = reconstructed['payload']; keys = section.split('.')
        for key in keys[:-1]: parent = parent[key]
        parent[keys[-1]] = ({k:observed[section][k] for k,_ in expected}
            if isinstance(section_value(payload,section),dict) else [observed[section][k] for k,_ in expected])
    if emit(reconstructed) != emit(source):
        raise ValueError('QA lossless reconstruction failed')
    result = {'complete':True, 'result_sha256':originals[name]['sha256'],
        'lexical_value_sha256':text_hash(emit(source)), 'reconstructed_sha256':text_hash(emit(reconstructed)),
        'section_counts':{k:len(v) for k,v in observed.items()}, 'views':len(views),
        'required_files':{p:originals[p]['sha256'] for p in required},
        'recovery_context_sha256':full.get('recovery_context',{}).get('sha256'),
        'max_content_chars':max(size(v['files']) for v in views),
        'claim_limit':'All selected values, order, multiplicity and numeric lexemes are conserved. '
                      'Original bytes remain hash-pinned. JSON string escape spelling/whitespace is not byte restoration. '
                      'This is transport coverage, not semantic QA or a research result.'}
    if history_coverage: result['supporting_results'] = history_coverage
    return result


def save_plan(state, full, views, coverage, maintenance_files, destination, lead_decision, *, reuse_plan=None):
    """Write an immutable plan after offline validation; no call or state mutation."""
    destination = Path(destination)
    validate_coverage(full, views)
    if size(maintenance_files) > MAX_CONTENT or any(v.get('truncated') for v in maintenance_files.values()):
        raise ValueError('Incomplete or oversized independent maintenance evidence')
    identity = {'version':VERSION, 'job_id':full['job_id'], 'coverage':coverage,
        'qa_authority':state['qa_authority'], 'model_route_authority':state.get('model_route_authority'),
        'lead_decision':lead_decision,
        'maintenance_hashes':{k:v['sha256'] for k,v in maintenance_files.items()}}
    plan_key = text_hash(json.dumps(identity, ensure_ascii=False, sort_keys=True))
    destination.mkdir(parents=True, exist_ok=False)
    write(destination/'full_collection.json',full)
    refs=[]
    for index,view in enumerate(views):
        path=destination/f'view_{index:03d}.json';write(path,view);refs.append(pin(path))
    maintenance={'files':maintenance_files, 'scope':
        'Independent acceptance of the Lead-directed QA transport maintenance only. Review current sources, '
        'scoped diffs, actual isolated test invocation/output, preservation hashes and complete coverage proof. '
        'Check lossless indices/number lexemes, missing/hash/truncation/dangling-reference fail-closed gates, '
        'accounting and every complete-view/aggregate acceptance gate. This does not accept the research job.'}
    write(destination/'maintenance.json',maintenance)
    reconciliation=json.loads(checked(state['operator_metadata_recovery']['reconciliation']).read_text(encoding='utf-8'))
    plan=dict(identity,plan_key=plan_key,full_collection=pin(destination/'full_collection.json'),
              views=refs,maintenance=pin(destination/'maintenance.json'),
              reconciliation=state['operator_metadata_recovery']['reconciliation'],
              verification=reconciliation['verification'],
              metadata_snapshot_only=True,worker_dispatch_authorized=False)
    if reuse_plan:
        plan['reuse_plan'] = reuse_plan
    write(destination/'plan.json',plan)
    return pin(destination/'plan.json')


def view_bundle(plan, full, view, name, *, maintenance=False, final=False):
    # Preserve an already paid review only by returning its exact original
    # bundle. Changed views, routes or context always receive a new identity.
    if plan.get('reuse_plan') and not maintenance and not final and name.startswith('view_'):
        prior = json.loads(checked(plan['reuse_plan']).read_text(encoding='utf-8'))
        prior_full = json.loads(checked(prior['full_collection']).read_text(encoding='utf-8'))
        index = int(name.removeprefix('view_'))
        same_context = (all(plan[k] == prior[k] for k in
                            ('job_id', 'qa_authority', 'model_route_authority'))
                        and all(full.get(k) == prior_full.get(k) for k in
                                ('qa_route', 'skill_guidance', 'recovery_context')))
        if same_context and index < len(prior['views']):
            previous = json.loads(checked(prior['views'][index]).read_text(encoding='utf-8'))
            if view == previous:
                return view_bundle(prior, prior_full, previous, name)
    key=text_hash(plan['plan_key']+name+json.dumps(view,ensure_ascii=False,sort_keys=True))
    bundle={'review_key':key,'job_id':plan['job_id'], 'files':view['files'],
        'required_complete_files':list(view['files']), 'deterministically_verified':True,
        'transport_plan_key':plan['plan_key'], 'transport_review_scope':view['scope'],
        'transport_maintenance':maintenance, 'transport_final':final,
        'qa_route':full['qa_route'], 'skill_guidance':full.get('skill_guidance'),
        'market_data_read_by_qa':False, 'live':'DISABLED', 'supplemental_saved_evidence':{},
        'scope':'Independent maintenance' if maintenance else 'Complete selected view; no standalone full-job acceptance',
        'snapshot_note':'This plan preserves its source/context snapshot. Current authority and accounting are checked separately by Controller.'}
    if len(json.dumps(bundle,ensure_ascii=False)) > 700000:
        raise ValueError('Complete QA view exceeds serialized prompt bound')
    return bundle


def load_plan(state, folder):
    from .safety_hold import delegated_authority, active
    ref=state['qa_transport_plan'];path=checked(ref)
    revisions=(ROOT/'research/campaigns/PAYOUT_V1/revisions').resolve()
    if not path.is_relative_to(revisions):
        raise ValueError('QA transport plan outside revisions')
    plan=json.loads(path.read_text(encoding='utf-8'))
    if plan['job_id'] != folder.name:
        return None
    if (plan['version']!=VERSION or plan['worker_dispatch_authorized'] is not False
            or active(state) or not delegated_authority(state)
            or plan['qa_authority']!=state['qa_authority']
            or plan['model_route_authority']!=state.get('model_route_authority')
            or plan['reconciliation']!=state['operator_metadata_recovery']['reconciliation']):
        raise ValueError('QA transport authority or repair identity changed')
    decision=json.loads(checked(plan['lead_decision']).read_text(encoding='utf-8'))
    if (decision['action']!='PAUSE' or decision.get('next_job') is not None
            or decision.get('worker_dispatch_ready') is not False):
        raise ValueError('QA transport maintenance requires a non-dispatching Lead decision')
    full=json.loads(checked(plan['full_collection']).read_text(encoding='utf-8'))
    if full.get('deterministically_verified') is not True:
        raise ValueError('QA transport lacks deterministic job verification')
    # An independently accepted diagnostic is a pinned dependency, not a new
    # obligation to repeat its old complete review in each unrelated market job.
    from .qa_review import accepted_historical_qa
    for jid,dependency in full.get('accepted_result_dependencies',{}).items():
        checked(dependency['result'])
        accepted=accepted_historical_qa(dict(state,evidence_id=jid,pending_job=None))
        if (not accepted or any(accepted.get(k)!=dependency['review'].get(k)
                for k in ('path','sha256','review_key','job_id','verdict'))
                or accepted.get('partial_review') or accepted.get('maintenance_review')
                or accepted['source_hashes'].get(dependency['result']['path'])!=dependency['result']['sha256']
                or dependency['review']['path'] not in full['required_complete_files']):
            raise ValueError('Accepted historical result dependency changed or incomplete')
    verification=json.loads(checked(plan['verification']).read_text(encoding='utf-8'))
    if verification.get('exit_code')!=0 or verification.get('preserved') is not True:
        raise ValueError('QA transport repair regression/preservation incomplete')
    checked(verification['stdout']);checked(verification['stderr'])
    if (not verification.get('source_after')
            or verification['source_before'] != verification['source_after']):
        raise ValueError('QA transport repair lacks unchanged tested source hashes')
    for source in verification['source_after']:checked(source)
    for historical in verification.get('history_pins',[]):checked(historical)
    from .qa_review import compact_json_text
    for name,sha in plan['coverage']['required_files'].items():
        source=checked({'path':name,'sha256':sha})
        content=source.read_text(encoding='utf-8')
        if source.suffix=='.json':content=compact_json_text(content)
        if content!=full['files'][name]['content']:
            raise ValueError('QA collected content differs from actual pinned source')
    views=[json.loads(checked(p).read_text(encoding='utf-8')) for p in plan['views']]
    if validate_coverage(full,views)!=plan['coverage']:
        raise ValueError('QA transport coverage manifest mismatch')
    maintenance=json.loads(checked(plan['maintenance']).read_text(encoding='utf-8'))
    for name,item in maintenance['files'].items():
        source=checked({'path':name,'sha256':item['sha256']})
        content=source.read_text(encoding='utf-8')
        if source.suffix=='.json':content=compact_json_text(content)
        if item.get('truncated') or content != item['content']:
            raise ValueError('QA maintenance content is not the complete pinned source')
    job_record=state.get('completed',{}).get(plan['job_id'],{})
    if job_record.get('sha256')!=plan['coverage']['result_sha256']:
        raise ValueError('QA transport completed result identity changed')
    from .qa_review import authority
    auth=authority(state)
    if not auth or any(full['qa_route'][key]!=auth[key] for key in ('model','reasoning')):
        raise ValueError('QA transport model route changed')
    recovery={'episodes':{k:v for k,v in state.get('recovery_episodes',{}).items() if v.get('job_id')==plan['job_id']},
              'latest_authorization':state.get('lead_recovery_authorizations',{}).get(plan['job_id'])}
    if json.dumps(recovery,ensure_ascii=False,sort_keys=True)!=full['recovery_context']['content']:
        raise ValueError('QA transport current recovery accounting changed')
    # Validate EVERY serialized view before reserving even the maintenance call.
    maintenance_bundle=view_bundle(plan,full,maintenance,'maintenance',maintenance=True)
    bundles=[view_bundle(plan,full,v,f'view_{i:03d}') for i,v in enumerate(views)]
    return plan,full,maintenance_bundle,bundles


def with_source_review(bundle, source_review):
    """Stable identity for a complete view plus its accepted baseline review."""
    bundle=copy.deepcopy(bundle)
    report_text=checked(source_review).read_text(encoding='utf-8')
    if len(report_text)>REVIEW_CONTEXT_RESERVE:
        raise RuntimeError('LEAD_PREREQUISITE: complete source review needs a larger reserved view context')
    bundle['files'][source_review['path']]=file_content(source_review['sha256'],report_text)
    bundle['required_complete_files'].append(source_review['path'])
    bundle['review_key']=text_hash(bundle['review_key']+source_review['sha256'])
    if size(bundle['files'])>MAX_CONTENT or len(json.dumps(bundle,ensure_ascii=False))>700000:
        raise RuntimeError('LEAD_PREREQUISITE: complete view plus accepted source review exceeds bound')
    return bundle


def retry_bundle(state, bundle):
    """Use only the existing authenticated retry for its exact failed view."""
    from .autonomous_recovery import qa_retry_context
    # A later sibling failure must not select this view's original failed key.
    # Reconstruct its accepted attempt only under the identical delivered bundle
    # and still-authentic historical retry; this creates no retry authority.
    for accepted in reversed(list(state.get('qa_reviews', {}).values())):
        if (accepted.get('verdict') != 'PASS'
                or accepted.get('transport_base_review_key') != bundle['review_key']
                or accepted.get('review_key') == bundle['review_key']):
            continue
        historical = accepted.get('transport_retry_context')
        if not historical:
            raise ValueError('Accepted QA retry lacks its authenticated context')
        historical_state = dict(state, autonomous_recovery=dict(
            state.get('autonomous_recovery', {}), qa_retries={bundle['job_id']: historical}))
        if qa_retry_context(historical_state, bundle['job_id']) != historical:
            raise ValueError('Accepted QA retry authority changed')
        restored = copy.deepcopy(bundle)
        restored.update(transport_base_review_key=bundle['review_key'], qa_retry_context=historical,
                        review_key=text_hash(bundle['review_key'] + json.dumps(historical, sort_keys=True)))
        if (restored['review_key'] != accepted.get('review_key')
                or accepted.get('transport_plan_key') != bundle.get('transport_plan_key')
                or accepted.get('transport_bundle_sha256') != text_hash(
                    json.dumps(restored, ensure_ascii=False, sort_keys=True))):
            raise ValueError('Accepted QA retry source/plan/dependency identity changed')
        from .qa_review import validate_report
        report = json.loads(checked(accepted).read_text(encoding='utf-8'))
        if validate_report(report, restored)['verdict'] != 'PASS':
            raise ValueError('Accepted QA retry no longer has a valid PASS')
        return restored
    retry = qa_retry_context(state, bundle['job_id'])
    if not retry:
        return bundle
    failed = state.get('qa_reviews', {}).get(retry['failed_review_key'], {})
    if (failed.get('transport_base_review_key', retry['failed_review_key'])
            != bundle['review_key']):
        return bundle
    report = json.loads(checked(retry['report']).read_text(encoding='utf-8'))
    if (any(failed.get(k) != retry['report'][k] for k in ('path', 'sha256'))
            or failed.get('job_id') != bundle['job_id']
            or failed.get('verdict') != 'INCOMPLETE' or report.get('verdict') != 'INCOMPLETE'
            or report.get('limitations') != ['Controller-generated failure envelope; no accepted model QA verdict.']):
        raise ValueError('QA view retry requires its preserved generated failure')
    changed = copy.deepcopy(bundle)
    changed['transport_base_review_key'] = bundle['review_key']
    changed['qa_retry_context'] = retry
    changed['review_key'] = text_hash(bundle['review_key'] + json.dumps(retry, sort_keys=True))
    return changed


def review_plan(controller, folder):
    """Independent maintenance first, then complete views, then independent synthesis."""
    state=controller.state
    loaded=load_plan(state,folder)
    if loaded is None:return None
    plan,full,maintenance,views=loaded
    from .qa_review import review_bundle
    from .runtime_policy import remaining_call_units
    from .skill_engineering import maybe_review
    cached=state.get('qa_reviews',{})
    maintenance=retry_bundle(state,maintenance)
    views[0]=retry_bundle(state,views[0])
    source_review=cached.get(views[0]['review_key'])
    budget_views=([retry_bundle(state,with_source_review(b,source_review)) for b in views[1:]]
                  if source_review and source_review['verdict']=='PASS' else views[1:])
    pending=sum(b['review_key'] not in cached for b in [maintenance,views[0]]+budget_views)
    # Reserve final synthesis + Lead and worst-case skill review/edit-review cadence.
    reserve=pending+2+2*((pending+11)//10)
    if remaining_call_units(state,controller.profile)<reserve:
        state['status']='RESOURCE_LIMIT';controller.save()
        raise RuntimeError('RESOURCE_LIMIT: complete QA views, skill cadence and mandatory Lead reserve')
    reports=[]
    for index, original in enumerate([maintenance]+views):
        bundle=copy.deepcopy(original)
        if index>=2:
            bundle=with_source_review(bundle,reports[1])
        bundle=retry_bundle(state,bundle)
        maybe_review(controller)
        # A skill checkpoint or an external actor cannot silently stale a plan.
        for name,sha in plan['coverage']['required_files'].items():checked({'path':name,'sha256':sha})
        review=review_bundle(controller,bundle)
        if review['verdict']!='PASS':
            return dict(review,blocked_source_hashes=plan['coverage']['required_files'],
                        transport_maintenance_review=reports[0] if reports else None)
        reports.append(review)
    # All independent views passed; the last independent reviewer reconciles
    # their findings and exact coverage. No Controller-generated research PASS.
    files={name:item for name,item in views[0]['files'].items()
           if plan['job_id'] in name or name.endswith('/generic_entry.py')}
    for review in reports:
        path=checked(review)
        from .qa_review import compact_json_text
        files[review['path']]=file_content(review['sha256'],compact_json_text(path.read_text(encoding='utf-8')))
    coverage_path=checked(state['qa_transport_plan'])
    files[state['qa_transport_plan']['path']]=file_content(digest(coverage_path),coverage_path.read_text(encoding='utf-8'))
    if size(files)>MAX_CONTENT:
        raise RuntimeError('LEAD_PREREQUISITE: complete QA synthesis exceeds supported bound')
    final=view_bundle(plan,full,{'files':files,'scope':
        'Final independent full-job synthesis. All exact complete views have separate independent PASS reports '
        'supplied in full, and the Controller has revalidated union coverage and every input hash. '
        'Reconcile their findings and all exact coverage populations, current versus historical comparisons, '
        'finite-data checks, original/corrected chronology, source correspondence, fixture/oracle derivations, '
        'every closed-trade money field/risk200, unresolved nulls and all permissions. '
        'The primary research source/static result are also supplied. Any contradiction, unreviewed material '
        'claim, missing independent acceptance or incomplete part blocks PASS. A simulation gives no market '
        'edge or cash/live authority.'},'synthesis',final=True)
    final=retry_bundle(state,final)
    maybe_review(controller)
    review=review_bundle(controller,final,publish=False)
    # The raw model verdict is still provisional. Check every original/view and
    # every report delivered to synthesis before exposing any full-job verdict.
    try:
        load_plan(state,folder)
        for name,item in final['files'].items():checked({'path':name,'sha256':item['sha256']})
        checked(review)
    except (ValueError, OSError, RuntimeError) as error:
        rejected=ROOT/review['path']
        rejection={'review_key':review['review_key'],'report':{k:review[k] for k in ('path','sha256')},
                   'accepted':False,'error':str(error),'raw_call_and_accounting_preserved':True}
        write(rejected.parent/'acceptance_rejected.json',rejection)
        state.get('qa_reviews',{}).pop(review['review_key'],None)
        state['latest_qa_review']=dict(
            {k:v for k,v in reports[-1].items() if k!='report'},
            blocked_source_hashes=plan['coverage']['required_files'])
        state.setdefault('qa_review_invalidations',{})[review['review_key']]=pin(rejected.parent/'acceptance_rejected.json')
        controller.save()
        raise RuntimeError('LEAD_PREREQUISITE: rejected final synthesis identity; no accepted full-job verdict') from error
    review.update(source_hashes=dict(review['source_hashes'],**plan['coverage']['required_files']),
                  transport_coverage=plan['coverage'],transport_view_reviews=[
                      {k:r[k] for k in ('path','sha256','review_key','verdict')} for r in reports],
                  transport_maintenance_review=reports[0],
                  partial_review=False,maintenance_review=False)
    review.pop('acceptance_pending',None)
    write((ROOT/review['path']).parent/'acceptance.json',{
        'report':{k:review[k] for k in ('path','sha256','review_key','verdict')},
        'plan':state['qa_transport_plan'],'source_hashes':review['source_hashes'],
        'post_call_original_view_and_report_identity_verified':True})
    saved={k:v for k,v in review.items() if k!='report'}
    state['qa_reviews'][review['review_key']]=saved;state['latest_qa_review']=saved
    state.setdefault('authorized_metadata_artifacts',{})[review['path']]={
        'sha256':review['sha256'],'partition':'POLICY_OR_METADATA'}
    controller.save()
    return review
