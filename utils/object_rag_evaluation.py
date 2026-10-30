"""Source sampling and resumable experiments for 06.test.rag.object.question_with_location.ipynb.

This is evaluation tooling, not part of the production retriever. References are
only used by question authors/reviewers; EvidenceReplay receives retrieved data.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import html
import inspect
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import time

from bs4 import BeautifulSoup
import pandas as pd
from ribosome.rag.object import ObjectRAG, canonical

SEED = 20261029
EVALUATION_VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def atomic_text(path, value):
    """Replace a result only after its complete, flushed text is on disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def read_json(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def plain(text):
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    return ' '.join(BeautifulSoup(text, 'html.parser').get_text(' ', strip=True)
                    .replace('**', '').split())


def source_units(path):
    """Find exact line-addressable rows and prose after substantive headings.

    Keep whole function entries when rows describe format/parameters/examples.
    Do not infer text from screenshots or use front matter as a question pool.
    """
    lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    units, headings, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r'^(#{1,6})\s+(.+)', line)
        if match:
            level, title = len(match[1]), plain(match[2])
            headings = [(n, t) for n, t in headings if n < level] + [(level, title)]
            i += 1
            continue
        valid = headings and re.match(r'^\d+\s', headings[0][1])
        if not valid or any(re.search(r'修订|目录|历次|版本记录', t) for _, t in headings):
            i += 1
            continue
        start, end = i, i
        row = '<tr' in line
        if row:
            while end < len(lines)-1 and '</tr>' not in lines[end]:
                end += 1
        elif (line.strip() and not line.lstrip().startswith(('<', '!['))
              and not re.match(r'^\s*(图|表)\s*\d', plain(line))):
            # List items are independent source facts; ordinary prose spans a paragraph.
            if not re.match(r'^\s*(\d+[.、)]|[-*])\s', line):
                while end+1 < len(lines) and lines[end+1].strip() and not re.match(
                        r'^(#|<|!\[|\s*\d+[.、)]\s)', lines[end+1]):
                    end += 1
        else:
            i += 1
            continue
        excerpt = '\n'.join(lines[start:end+1])
        text = plain(excerpt)
        if len(text) >= 12 and len(text) <= 4000 and not re.match(
                r'^(目录|图\s*\d|表\s*\d|分析结果$)', text):
            cells = [c.get_text(' ', strip=True) for c in
                     BeautifulSoup(excerpt, 'html.parser').find_all(['td', 'th'])]
            kind = ('alarm' if row and re.fullmatch(r'\d{3,8}(?:[-.]\d+)?', cells[0] if cells else '')
                    and any(re.search(r'报警|故障|错误', t) for _, t in headings)
                    else 'instruction' if re.search(r'指令|格式|语法|函数|接口', text+' '+headings[-1][1])
                    else 'prerequisite' if re.search(r'注意|必须|只有|条件|禁止', text)
                    else 'parameter' if re.search(r'参数|范围|单位|取值', text)
                    else 'procedure')
            units.append(dict(line_start=start+1, line_end=end+1, excerpt=excerpt,
                              heading_path=[t for _, t in headings], kind=kind,
                              representation='table_row' if row else 'prose',
                              text=text))
        i = end+1
    # Deduplicate repeated entries without losing the first exact locator.
    unique = {}
    for unit in units:
        unique.setdefault(unit['text'], unit)
    return list(unique.values())


def sample_units(path, source_path, count=20):
    units = source_units(path)
    seed = SEED + int(hashlib.sha256(source_path.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    # Round-robin chapters and kinds prevents large alarm tables from dominating.
    groups = defaultdict(list)
    for unit in units:
        groups[(unit['heading_path'][0], unit['kind'])].append(unit)
    keys = sorted(groups)
    rng.shuffle(keys)
    for pool in groups.values():
        rng.shuffle(pool)
    selected = []
    while len(selected) < min(count, len(units)):
        for key in keys:
            if groups[key] and len(selected) < count:
                selected.append(groups[key].pop())
    # Short manuals may need several different tasks grounded in the same prose.
    # The author sees these explicitly, and must avoid paraphrasing another case.
    if len(selected) < count and selected:
        base = list(selected)
        selected.extend(deepcopy(base[i % len(base)]) for i in range(count-len(selected)))
    if len(selected) != count:
        raise ValueError(f'No substantive source pool: {source_path}')
    return seed, len(units), selected


def csv_rows(root):
    with (Path(root)/'assets/code-docs/product_manuals_markdown.csv').open(
            encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def mapping_snapshot(app, docs):
    return dict(categories={str(k): str(v) for k, v in app.categories.items()},
                entries=list(getattr(app, 'mapping', [])),
                indexed=sorted(docs.values(), key=digest))


def cache_readonly_snapshots(app):
    """Memoize production snapshot results while active generations stay fixed.

    This only avoids repeated SQLite/JSON decoding. Candidate eligibility,
    ordering, category coverage, vectors and source content are unchanged.
    """
    if getattr(app.snapshot, '_evaluation_snapshot_cache', False):
        return app.snapshot
    original = app.snapshot
    views, common_records = {}, None

    def signature():
        return (tuple(app.connection.execute('SELECT document_id,generation_id FROM active_documents ORDER BY document_id')),
                tuple(app.connection.execute('SELECT key,value FROM settings ORDER BY key')),
                digest(dict(categories=app.categories, mapping=getattr(app, 'mapping', []))))

    frozen = signature()

    def snapshot(category, **options):
        nonlocal common_records
        if signature() != frozen:
            raise ValueError('Active index changed during evaluation; resume with its new identity')
        key = (category, tuple(sorted(options.items())))
        if key not in views:
            records, docs, coverage = original(category, **options)
            if signature() != frozen:
                raise ValueError('Active index changed while reading the snapshot')
            if common_records is None:
                common_records = records
            # Preserve original iteration order (and therefore query batches).
            if records.keys() == common_records.keys() and list(records) == list(common_records):
                records = common_records
            views[key] = (records, docs, coverage)
        return views[key]

    snapshot._evaluation_snapshot_cache = True
    app.snapshot = snapshot
    return snapshot


class QueryEmbeddingCache:
    """Replay model-produced query vectors, with model/config/dimension isolation.

    Only query strings are sent to the embedder. This changes request scheduling,
    not the production retrieval algorithm or its vector-space validation.
    """
    def __init__(self, client, model, config, dimension, directory):
        self.client, self.model, self.dimension = client, model, dimension
        self.identity = digest(dict(model=model, config=config, dimension=dimension,
                                    endpoint=str(getattr(getattr(client, '_client', None), 'base_url', ''))))
        self.directory = Path(directory)/self.identity[:16]

    def path(self, question):
        return self.directory/(digest(question)+'.json')

    def load(self, question):
        record = read_json(self.path(question))
        if record is None:
            return None
        if record['identity'] != self.identity or record['question'] != question:
            raise ValueError('Query embedding cache identity mismatch')
        self.validate(record['embedding'])
        return record['embedding']

    def validate(self, vector):
        if (len(vector) != self.dimension or not all(math.isfinite(x) for x in vector)
                or not any(vector)):
            raise ValueError('Invalid cached query embedding')

    def save(self, question, vector):
        self.validate(vector)
        atomic_json(self.path(question), dict(identity=self.identity, question=question, embedding=vector))

    async def prepare(self, questions, *, batch_size=16):
        pending = [q for q in dict.fromkeys(questions) if self.load(q) is None]
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start+batch_size]
            response = await self.client.embed(model=self.model, input=batch)
            vectors = response['embeddings']
            if len(vectors) != len(batch):
                raise ValueError('Embedding response count mismatch')
            for vector in vectors:
                self.validate(vector)
            for question, vector in zip(batch, vectors):
                self.save(question, vector)
            print(f'Query vectors [{min(start+len(batch),len(pending))}/{len(pending)}]', flush=True)

    async def embed(self, *, model, input):
        if model != self.model or not isinstance(input, str):
            raise ValueError('Cached embedder requires the configured model and one query string')
        vector = self.load(input)
        if vector is None:
            response = await self.client.embed(model=model, input=input)
            vector = response['embeddings'][0]
            self.save(input, vector)
        return dict(embeddings=[vector])


class EvidenceReplay(ObjectRAG):
    """Production answer method, with frozen retrieval and recorded responses."""
    def __init__(self, app, evidence, top_k):
        self.rag_model, self.max_output_tokens = app.rag_model, app.max_output_tokens
        self.evidence, self.top_k, self.last_result = evidence, top_k, None
        self.responses = []
        owner = self

        class Recorder:
            async def chat(self, **kwargs):
                response = await app.generator.chat(**kwargs)
                owner.responses.append(response['message']['content'])
                return response

        self.generator = Recorder()

    async def retrieve(self, question, category, *, top_k=8):
        if (question, category, top_k) != (
                self.evidence['question'], self.evidence['category'], self.top_k):
            raise ValueError('Replay must use the original question/category/top-k')
        self.last_result = deepcopy(self.evidence)
        return self.last_result


def source_key(case):
    if case.get('source_md'):
        return canonical(case['source_md'])[0]
    ref = case['references'][0]
    docx = Path(str(ref['docx']).replace('\\', '/'))
    # Conversion writes <stem>/<stem>.md next to the DOCX's relative directory.
    markdown = docx.parent / docx.stem / (docx.stem+'.md')
    return canonical(str(markdown).replace(
        '/06 产品手册/', '/.md/06 产品手册/'))[0]


def expected_hit(case, hit):
    key = hit['manual'].get('key') or canonical(hit['record']['source_file'])[0]
    return key == source_key(case) and (
        not case.get('profile') or hit['manual'].get('profile') == case['profile'])


def heading_title(text):
    return re.sub(r'\s+', '', re.sub(r'^\s*\d+(?:\.\d+)*\s+', '', text.strip('* '))).translate(
        str.maketrans({'—': '-', '–': '-', '－': '-'}))


def heading_matches(expected, actual):
    remaining = iter(heading_title(t) for t in actual)
    return all(any(t == heading_title(wanted) for t in remaining) for wanted in expected)


def anchors_match(anchors, text):
    return all(re.search(r'(?<![A-Za-z0-9_])'+re.escape(term)+r'(?![A-Za-z0-9_])',
                         text, re.I) for term in anchors) if anchors else None


def context_blocks(result):
    return re.findall(r'(?ms)^\[(E\d+)\] (.*?)(?=^\[E\d+\] |\Z)', result.get('context', ''))


def review_text(text):
    """Render known HTML formatting without removing command placeholders."""
    tags = r'table|tbody|tr|td|th|p|div|span|strong|b|em|i|ul|ol|li|br|pre|code|sub|sup|a'

    def replace(match):
        closing, tag = match.group(1), match.group(2)
        if tag in ('td', 'th') and not closing:
            return ' | '
        if tag == 'br' or closing and tag in ('table', 'tr', 'p', 'div', 'li', 'pre'):
            return '\n'
        return ''

    return html.unescape(re.sub(r'<(/?)('+tags+r')(?:\s+[^>]*|\s*)/?>', replace, text)).strip()


def review_input(case, state):
    return dict(question=case['question'], expected_revision=case.get('revision'),
                expected_source=case.get('source_md'), expected_corpus_profile=case.get('profile'),
                expected_facts=case.get('expected_facts', []),
                references=[review_text(r['excerpt']) for r in case['references']],
                supplied_evidence=review_text(state.get('retrieval', {}).get('context', '')),
                source_rendering='HTML formatting removed; placeholders, text and row order retained; originals saved in case and retrieval records',
                answer=state.get('answer', {}).get('answer', ''),
                code_example_status=state.get('answer', {}).get('example_status', 'not generated'))


def compatible_review(case, state):
    review = state.get('review', {})
    if review.get('answer_digest') != digest(review_input(case, state)):
        return {}
    if (review.get('method') == 'model-assisted' and review.get('raw_review') == ''
            and state.get('answer_status') != 'complete'):
        return dict(review, method='execution-diagnostic')
    return review


def case_metrics(case, state, available_keys):
    result = state.get('retrieval', {})
    matching = [h for h in result.get('hits', []) if expected_hit(case, h)]
    # Omitted hits are still in result['hits']; only context blocks reach the model.
    def payload_for(hit, block):
        header, _, payload = block.partition('\n')
        if (header.endswith(' | '+hit['record']['id']) and hit['record']['source_file'] in header
                and payload.strip() not in ('', '[]')):
            return payload
        return ''

    supplied = [h for h in matching if any(payload_for(h, block) for _, block in context_blocks(result))]
    path = case.get('heading_path', [])
    section = case.get('section', '')
    section_hit = any(heading_matches(path, h['record'].get('section_headings', []))
                      if path else any(t.split(maxsplit=1)[0] == section
                                       for t in h['record'].get('section_headings', []) if t.strip())
                      for h in matching) if path or section else None
    context_section_hit = any(heading_matches(path, h['record'].get('section_headings', []))
                             if path else any(t.split(maxsplit=1)[0] == section
                                 for t in h['record'].get('section_headings', []) if t.strip())
                             for h in supplied) if path or section else None
    answer = state.get('answer', {})
    return dict(case_id=case['id'], category=case['category'],
                source=case.get('source_md', case['references'][0].get('docx', '')),
                revision=case.get('revision', ''), kind=case['kind'],
                representation=case.get('representation', 'table_row'),
                corpus_available=source_key(case) in available_keys,
                retrieval_complete=state.get('retrieval_status') == 'complete',
                hits=len(result.get('hits', [])), expected_manual_hit=bool(matching),
                expected_source_hits=len(matching), expected_source_context_hits=len(supplied),
                cross_category_hits=sum(h['manual'].get('category', case['category']) != case['category']
                                        for h in result.get('hits', [])),
                expected_source_in_context=bool(supplied), expected_section_hit=section_hit,
                expected_section_in_context=context_section_hit,
                anchors_in_matching_hits=anchors_match(case['anchors'],
                    '\n'.join(plain(h['record']['content']) for h in matching)) if case.get('anchors') else None,
                anchors_in_context=anchors_match(case['anchors'],
                    '\n'.join(plain(payload_for(h, block)) for _, block in context_blocks(result)
                              for h in supplied if payload_for(h, block))) if case.get('anchors') else None,
                context_chars=len(result.get('context', '')), omissions=len(result.get('omissions', [])),
                answer_complete=state.get('answer_status') == 'complete',
                example_status=answer.get('example_status', 'not generated'),
                retrieval_error=state.get('retrieval_error', ''), answer_error=state.get('answer_error', ''),
                assessment=compatible_review(case, state).get('assessment', 'not reviewed'))


class EvaluationRun:
    def __init__(self, app, cases, directory, *, top_k=8, settings=None):
        self.app, self.cases, self.top_k = app, cases, top_k
        self.root_directory, self.settings = Path(directory), settings
        active = sorted(app.connection.execute('SELECT document_id,generation_id FROM active_documents'))
        docs = app.snapshot(next(iter(app.categories)), all_categories=True)[1]
        self.available_keys = {d['key'] for d in docs.values()}
        mapping = mapping_snapshot(app, docs)
        mapping_digest = digest(mapping)
        identity = dict(version=EVALUATION_VERSION, active=active,
                        catalog=list(app.connection.execute('SELECT key,value FROM settings ORDER BY key')),
                        model=app.rag_model, embed_model=app.embed_model,
                        embedding_config=app.embedding_config, top_k=top_k,
                        context_limit=app.max_context_chars, output_limit=app.max_output_tokens,
                        answer_implementation=inspect.getsource(ObjectRAG),
                        replay_implementation=inspect.getsource(EvidenceReplay), settings=settings or {})
        base_id = digest(identity)
        base_manifest = read_json(Path(directory)/base_id[:16]/'run.json')
        # Keep compatible legacy runs; a changed mapping gets its own namespace.
        if base_manifest and base_manifest.get('mapping_digest') != mapping_digest:
            identity['mapping_digest'] = mapping_digest
        self.run_id = digest(identity)
        self.directory = Path(directory)/self.run_id[:16]
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest_path = self.directory/'run.json'
        manifest = read_json(manifest_path)
        if manifest is None:
            atomic_json(manifest_path, dict(run_id=self.run_id, identity=identity, started_at=time.time(),
                                           mapping_digest=mapping_digest, mapping_snapshot=mapping))
        elif (manifest['run_id'] != self.run_id or manifest['identity'] != json.loads(json.dumps(identity))
              or manifest.get('mapping_digest') != mapping_digest):
            raise ValueError('Evaluation directory identity mismatch')
        self.states = {}
        if cases:
            self.register(cases)

    def path(self, case):
        return self.directory/(case['id']+'-'+digest(case)[:12]+'.json')

    def load(self, case):
        state = read_json(self.path(case), {})
        if state and (state.get('case_digest') != digest(case) or state.get('run_id') != self.run_id):
            raise ValueError('Saved case identity mismatch; previous output preserved')
        self.states[case['id']] = state
        return state

    def save(self, case, state):
        atomic_json(self.path(case), state)
        self.states[case['id']] = state

    def for_top_k(self, top_k):
        if top_k == self.top_k:
            return self
        return EvaluationRun(self.app, self.cases, self.root_directory,
                             top_k=top_k, settings=self.settings)

    async def retrieve_case(self, case):
        state = self.load(case)
        state.update(case_id=case['id'], case_digest=digest(case), run_id=self.run_id)
        if state.get('retrieval_status') != 'complete':
            try:
                evidence = await self.app.retrieve(case['question'], case['category'], top_k=self.top_k)
                state.update(retrieval=evidence, retrieval_status='complete', retrieval_error='')
                state.pop('answer_status', None)
            except Exception as error:
                state.update(retrieval_status='error', retrieval_error=f'{type(error).__name__}: {error}')
            self.save(case, state)
        return state

    async def retrieve_cases(self, cases):
        self.register(cases)
        for case in cases:
            await self.retrieve_case(case)
        return self.metrics(cases)

    def register(self, cases):
        bank = {c['id']: c for c in self.cases}
        for case in cases:
            bank[case['id']] = case
        self.cases = list(bank.values())
        if self.cases:
            self.case_bank_digest = digest(self.cases)
            self.case_bank_path = self.directory/'case-banks'/(self.case_bank_digest[:16]+'.json')
            if not self.case_bank_path.exists():
                atomic_json(self.case_bank_path, dict(case_bank_digest=self.case_bank_digest, cases=self.cases))

    async def run_case(self, case):
        state = await self.retrieve_case(case)
        if state.get('retrieval_status') == 'complete' and state.get('answer_status') != 'complete':
            replay = EvidenceReplay(self.app, state['retrieval'], self.top_k)
            try:
                answer = await replay.answer(case['question'], case['category'], top_k=self.top_k)
                invalid = answer.get('example_status') == 'unavailable'
                state.update(answer=answer, answer_status='error' if invalid else 'complete',
                             answer_error=('Response validation failed: '+answer.get('example_error', 'invalid example'))
                             if invalid else '',
                             generation_responses=replay.responses)
            except BaseException as error:
                # Retain the frozen retrieval and any available stored summaries.
                state.update(answer_status='error', answer_error=f'{type(error).__name__}: {error}',
                             partial_answer=replay.last_result, generation_responses=replay.responses)
                self.save(case, state)
                if not isinstance(error, Exception):
                    raise
            self.save(case, state)
        return state

    async def run(self, cases=None, *, concurrency=2):
        cases = self.cases if cases is None else cases
        self.register(cases)
        semaphore = asyncio.Semaphore(concurrency)
        completed = 0

        async def work(case):
            nonlocal completed
            async with semaphore:
                state = await self.run_case(case)
                completed += 1
                print(f'[{completed}/{len(cases)}] {case["id"]}: '
                      f'{state.get("retrieval_status")} / {state.get("answer_status", "not attempted")}', flush=True)

        await asyncio.gather(*(work(case) for case in cases))
        return self.metrics(cases)

    def metrics(self, cases=None):
        return pd.DataFrame([case_metrics(case, self.load(case), self.available_keys)
                             for case in (self.cases if cases is None else cases)])

    def inspect(self, case_id):
        from IPython.display import Markdown, display
        case = next(c for c in self.cases if c['id'] == case_id)
        state = self.load(case)
        display(Markdown('### '+case_id+'\n\n'+case['question']))
        display(pd.DataFrame(case['references']))
        print(state.get('retrieval', {}).get('context', 'Retrieval not completed'))
        result = state.get('answer') or state.get('partial_answer') or {}
        display(Markdown('### Retrieved summary\n\n'+result.get('retrieved_summary', 'Unavailable')))
        display(Markdown('### Answer\n\n'+result.get('answer', state.get('answer_error', 'Not completed'))))
        display(compatible_review(case, state))

    def show_answers(self, cases):
        from IPython.display import Markdown, display
        for case in cases:
            state = self.load(case)
            if not state:
                continue
            for text in answer_blocks(case, state):
                display(Markdown(text))


def answer_blocks(case, state):
    """Display artifacts only; no reviewer references are passed to generation."""
    result = state.get('answer') or state.get('partial_answer') or {}
    blocks = ['### '+case['id']+'\n\n'+case['question'],
              '#### Retrieved content summary\n\n'+result.get('retrieved_summary', 'Not available'),
              '#### Answer\n\n'+result.get('answer', state.get('answer_error',
                  state.get('retrieval_error', 'Not completed')))]
    review = compatible_review(case, state)
    if review:
        title = 'Execution diagnostic' if review.get('method') == 'execution-diagnostic' else 'Model-assisted source review'
        blocks.append('#### '+title+'\n\n'
                      f'Assessment: **{review["assessment"]}**; completeness: {review["completeness"]}; '
                      f'citation support: {review["citation_support"]}; revision fidelity: {review["revision_fidelity"]}.\n\n'
                      +review['rationale'])
    return blocks


def grouped_metrics(frame, columns):
    rows = []
    for key, group in frame.groupby(columns, dropna=False):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(columns, key))
        row.update(cases=len(group), indexed_cases=int(group.corpus_available.sum()),
                   retrieval_completed=int(group.retrieval_complete.sum()),
                   expected_source_hits=int(group.expected_manual_hit.sum()),
                   expected_source_in_context=int(group.expected_source_in_context.sum()),
                   answers_completed=int(group.answer_complete.sum()),
                   generation_errors=int((group.answer_error != '').sum()))
        rows.append(row)
    return pd.DataFrame(rows)
