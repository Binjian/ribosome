"""CLI for authoring, running and publishing the notebook evaluation.

Run from the repository root with .venv/bin/python. All model calls are explicit
CLI actions; importing the support module or viewing case banks makes no calls.
"""
import argparse
import asyncio
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nbformat
from object_rag_evaluation import (
    EvaluationRun, QueryEmbeddingCache, answer_blocks, atomic_json, atomic_text, case_metrics, csv_rows, digest, grouped_metrics,
    plain, read_json, sample_units, source_key, compatible_review, cache_readonly_snapshots,
)

NOTEBOOK = ROOT/'nbs/06.test.rag.object.ipynb'
AUTHOR_DIR = ROOT/'db/object_rag_tests/authored'

# This very short manual has only sixteen text units. These twenty different
# tasks use its documented facts without asking the model to infer screenshots.
LOAD_QUESTIONS = [
    ('机器人查询', '如何通过名称关键字和安装方式查找机器人，支持哪些触发查询的方法？'),
    ('机器人列表', '进行负载计算前必须先完成哪一步机器人选择操作？'),
    ('机器人列表', '在机器人列表中选中型号后，界面会显示什么信息？'),
    ('负载数据输入', '负载数据输入区域支持输入机器人哪些部位的负载数据？'),
    ('负载分析：', '负载数据输入完成后，怎样启动负载合格性计算？'),
    ('清空结果', '怎样清空已经计算过的负载分析结果？'),
    ('机器人图形显示', '机器人图形显示区包含哪些图形，它们有助于理解什么？'),
    ('计算结果显示', '计算结果区能够回答哪些负载范围和使用需求方面的问题？'),
    ('红色表示', '负载分析结果为红色时表示什么状态？'),
    ('红色表示', '负载分析结果为黄色时表示什么状态，手册给出的临界阈值是多少？'),
    ('红色表示', '负载分析结果为绿色时表示什么状态？'),
    ('状态栏', '状态栏显示什么信息，把鼠标移到界面区域时能获得什么帮助？'),
    ('将对应值添加', '工具数模与机器人法兰坐标轴一致时，应怎样录入参数并查看分析结果？'),
    ('填写负载质量', '工具数模坐标轴与法兰坐标轴不一致时，应以哪个坐标系填写质量、质心和惯量？'),
    ('将对应值添加', '工具数模与法兰坐标轴一致时，填写负载参数需要注意哪些量的单位？'),
    ('填写负载质量', '工具数模与法兰坐标轴不一致时，选择坐标值后应执行什么操作确认负载结果？'),
    ('分析结果显示合格', '分析结果显示合格时，应如何判断机器人是否满足使用要求？'),
    ('分析结果显示合格', '分析结果显示不合格时，应如何判断当前机器人能否满足使用要求？'),
    ('工具质心惯量', '手册在工具数模示意图旁标注的工具质心惯量以什么位置为基准？'),
    ('工具质心偏移值', '手册在工具数模示意图旁标注的工具质心偏移以什么位置为基准？'),
]


def offline_author():
    """Create locally authored, source-specific questions without data transfer."""
    from object_rag_evaluation import source_units
    for row in csv_rows(ROOT):
        path = ROOT/row['path']
        seed, population, units = sample_units(path, row['path'])
        identity = author_identity(row, units)
        target = AUTHOR_DIR/(identity+'.json')
        if target.exists():
            continue
        questions = []
        if row['category'] == 'load_analysis':
            pool = source_units(path)
            units = [deepcopy_unit(next(u for u in pool if needle in u['text']))
                     for needle, _ in LOAD_QUESTIONS]
            questions = [q for _, q in LOAD_QUESTIONS]
        cases, used = [], set()
        for number, unit in enumerate(units, 1):
            text = unit['text']
            cells = [c.get_text(' ', strip=True) for c in
                     __import__('bs4').BeautifulSoup(unit['excerpt'], 'html.parser').find_all(['td', 'th'])]
            heading = re.sub(r'^\d+(?:\.\d+)*\s*', '', unit['heading_path'][-1])
            label = next((t for t in cells[:3] if len(t) >= 3 and not re.fullmatch(r'[\d.]+', t)
                          and t not in ['功能', '格式', '说明', '举例', '参数', '备注']), '')
            if not label:
                label = re.sub(r'^\d+[.、)]\s*', '', text)
                label = re.split(r'[：:，,。；;]', label, maxsplit=1)[0]
            label = label[:48].strip(' *') or heading
            tokens = re.findall(r'(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9_#]{2,}|[a-zA-Z]+_[a-zA-Z0-9_]+)(?![A-Za-z0-9_])', text)
            anchors = list(dict.fromkeys(tokens))[:2] if unit['kind'] == 'instruction' else []
            if unit['kind'] == 'alarm':
                anchors = [cells[0]]
                question = f'“{heading}”列出的报警{cells[0]}表示什么，原文给出了哪些原因或处理办法？'
            elif unit['kind'] == 'instruction':
                question = f'“{heading}”中“{label}”的功能或用法是什么？请说明本条记录实际给出的语法、参数或行为，不补写未载明的代码。'
            elif unit['kind'] == 'parameter':
                question = f'配置“{heading}”时，“{label}”对应哪些参数含义、取值或单位要求？只说明本条记录明确列出的内容。'
            elif unit['kind'] == 'prerequisite':
                question = f'操作“{heading}”时，涉及“{label}”的前提条件或注意事项是什么？'
            else:
                question = f'在“{heading}”中，应如何理解或操作“{label}”？请解释该项的操作方法、用途或结果。'
            if questions:
                question = questions[number-1]
            question = f'根据《{row["file_name"].removesuffix(".md")}》（{row["revision"]}），'+question
            if question in used:
                question += '本题关注原文记录“'+text[:70]+'”。'
            assert question not in used
            used.add(question)
            facts = [f.strip() for f in re.split(r'[。；]', text) if len(f.strip()) >= 5][:3]
            facts = facts or [text]
            cases.append(dict(id=f"sample_{row['category']}_{digest(row['path'])[:10]}_{number:02d}",
                category=row['category'], kind=unit['kind'], profile='', revision=row['revision'],
                section=unit['heading_path'][-1].split()[0], section_title=unit['heading_path'][-1],
                heading_path=unit['heading_path'], source_md=row['path'], source_version=row['version'],
                question=question, anchors=anchors, expected_facts=facts,
                representation=unit['representation'], references=[dict(markdown=row['path'],
                line_start=unit['line_start'], line_end=unit['line_end'], excerpt=unit['excerpt'],
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())]))
        atomic_json(target, dict(identity=identity, row=row, seed=seed, population=population,
            sampled_units=units, author_model='local source-derived questions; curated load-analysis tasks', cases=cases))
        print('Authored offline', row['category'], row['revision'], flush=True)


def deepcopy_unit(unit):
    return json.loads(json.dumps(unit, ensure_ascii=False))


def refine_local_cases(row, cases):
    """Make sampled program and menu questions ask distinct substantive tasks."""
    from bs4 import BeautifulSoup
    for case in cases:
        excerpt = case['references'][0]['excerpt']
        text = plain(excerpt)
        body = None
        needles = []
        if text.startswith('配置路径：'):
            target = re.split(r'->|→', text)[-1].strip('；;。 ')
            body = f'要在“{case["section_title"]}”中配置“{target}”，应按什么菜单路径进入？'
            case['kind'] = 'procedure'
        elif text.startswith('NOP'):
            case['kind'] = 'procedure'
            if 'GLUEL' in text:
                body = '涂胶流程模板怎样选择胶机系统、初始化胶枪，并完成沿轨迹开胶、关胶和循环结束？'
                needles = ['GlueSetSysNum', 'GlueInit', 'GLUEON', 'GLUEOFF', 'GlueEnd']
            elif 'USERALM' in text:
                body = '涂胶流程模板在冲洗或填充之前如何检查运行模式和胶桶状态，胶量不足时怎样处理？'
                needles = ['IN#38', 'I498', 'IN#71', 'USERALM']
            elif 'SELECT' in text and 'R494' in text:
                body = '冲洗作业模板怎样依据R494选择不排胶、强制排胶或根据请求排胶的分支？'
                needles = ['SELECT', '== 2.0', '== 3.0', 'IN#42']
            elif 'GLUEPURGE' in text:
                body = '胶机启动模板如何等待自动模式及加热器、泵就绪，并调用冲洗和填充作业？'
                needles = ['IN#65', 'IN#69', 'IN#66', 'GLUEPURGE', 'GLUEFILL']
            elif 'LASERL' in text:
                body = '激光焊模板怎样把起弧和收弧附加到LASERL运动指令上，起点和终点如何安排？'
                needles = ['LASERL']
            elif ':LASERWELDON' in text:
                body = '激光焊模板怎样在运动语句上附加起弧和收弧动作，所示时间参数分别是什么？'
                needles = [':LASERWELDON', ':LASERWELDOFF']
            elif 'LASERWELDON' in text:
                body = '激光焊模板中怎样用变量选择焊接文件并分别执行起弧和收弧，二者之间有哪些运动？'
                needles = ['LASERWELDON', 'LASERWELDOFF', 'MOVL P4']
            elif 'LASERON' in text:
                body = '激光焊模板如何使用独立的LASERON和LASEROFF指令安排起弧、焊接运动和收弧？'
                needles = ['LASERON', 'LASEROFF', 'MOVL P4']
            elif 'A03_MH' in text:
                body = '搬运模板在抓件前如何确认工具和区域，并检查工件、夹头和吸盘的状态？'
                needles = ['CheckTool', 'UserRequest', 'NoPart', 'AllOpen', 'AllVacOff']
            else:
                commands = list(dict.fromkeys(re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', text)))
                body = '作业模板中'+('、'.join(commands[1:4]) or '主要语句')+'的执行顺序和作用是什么？'
                needles = commands[1:4]
        if body:
            case['question'] = f'根据手册“{row["file_name"]}”（{row["revision"]}），'+body
            rows = [plain(str(tr)) for tr in BeautifulSoup(excerpt, 'html.parser').find_all('tr')]
            facts = [r for r in rows if any(needle in r for needle in needles)]
            if facts:
                case['expected_facts'] = facts
                case['anchors'] = [needle for needle in needles if needle in text]
        assert all(fact in text for fact in case['expected_facts'])
    assert len({c['question'] for c in cases}) == len(cases)
    return cases


def author_identity(row, units):
    return digest(dict(row=row, units=units, author_version=1))



def build():
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    groups, metadata = defaultdict(list), defaultdict(list)
    for row in csv_rows(ROOT):
        seed, population, units = sample_units(ROOT/row['path'], row['path'])
        authored = read_json(AUTHOR_DIR/(author_identity(row, units)+'.json'))
        if authored is None:
            raise FileNotFoundError('Author this source first: '+row['path'])
        if authored['author_model'].startswith('local source-derived'):
            authored['cases'] = refine_local_cases(row, authored['cases'])
        groups[row['category']].extend(authored['cases'])
        metadata[row['category']].append(dict(source_md=row['path'], seed=seed, population=population,
            authoring=authored['author_model'],
            selection='curated distinct short-manual tasks' if row['category'] == 'load_analysis' else 'seeded chapter/kind round-robin',
            selected_lines=[(u['line_start'], u['line_end']) for u in authored['sampled_units']]))
    start = next(i for i,c in enumerate(notebook.cells) if c.id == 'sampled-manual-tests')
    notebook.cells = notebook.cells[:start]
    notebook.cells.append(nbformat.v4.new_markdown_cell(
        '## CSV manual sampling tests\n\nThe following 18 families contain **760 new cases**, '
        '**20 per CSV file**. Revisions are grouped by family. Samples span substantive chapters '
        'and available task types. Original prose and HTML rows are retained as reviewer references. '
        'References and expected facts never enter the answer prompt. Manual revision and RC corpus '
        'profile are separate: CSV cases match canonical source paths across either corpus.\n\n'
        'Questions were authored locally from the fixed source samples; expected facts and anchors '
        'are literal source text. The shortest manual has 16 source units and uses 20 curated, '
        'distinct tasks within those units. Its manifest records that selection explicitly.', id='sampled-manual-tests'))
    notebook.cells.append(nbformat.v4.new_code_cell(
        '# | notest\nSAMPLED_CASES = []\nSAMPLING_MANIFEST = []\nSAMPLED_BANKS = {}\n'
        'def register_sample_bank(category, cases, manifest):\n'
        '    SAMPLED_BANKS[category] = (cases, manifest)\n'
        '    SAMPLED_CASES[:] = [case for bank, _ in SAMPLED_BANKS.values() for case in bank]\n'
        '    SAMPLING_MANIFEST[:] = [entry for _, entries in SAMPLED_BANKS.values() for entry in entries]\n',
        id='sampled-banks-start'))
    for category, cases in groups.items():
        notebook.cells.extend([
            nbformat.v4.new_markdown_cell(f'## {category.replace("_", " ").title()} — sampled manual revisions\n\n'
                f'{len(cases)} cases from {len(metadata[category])} CSV files. '
                f'Set `{category.upper()}_SAMPLE_IDS` to select case IDs; `None` selects all revisions. '
                'Run the batch cell explicitly to retrieve and answer; compatible completed results resume.',
                id=f'sampled-{category}-heading'),
            nbformat.v4.new_code_cell('# | notest\n'+category.upper()+'_SAMPLE_CASES = '+repr(cases)+'\n'
                'register_sample_bank('+repr(category)+', '+category.upper()+'_SAMPLE_CASES, '
                +repr(metadata[category])+')\n', id=f'sampled-{category}-bank'),
            nbformat.v4.new_code_cell('# | notest\n'+category.upper()+'_SAMPLE_IDS = None\n'
                f'{category}_sample_cases = select_cases({category.upper()}_SAMPLE_CASES, {category.upper()}_SAMPLE_IDS)\n'
                f'display(pd.DataFrame({category}_sample_cases)[["id", "revision", "kind", "question"]])\n',
                id=f'sampled-{category}-selection'),
            nbformat.v4.new_code_cell('# | notest\n'
                f'await query_embeddings.prepare([case["question"] for case in {category}_sample_cases])\n'
                f'{category}_sample_metrics = await evaluation.run({category}_sample_cases, concurrency=EVALUATION_CONCURRENCY)\n'
                f'{category}_sample_results = {{case["id"]: evaluation.load(case) for case in {category}_sample_cases}}\n'
                f'display({category}_sample_metrics)\n'
                f'evaluation.show_answers({category}_sample_cases)\n'
                f'# Inspect a complete result: evaluation.inspect({category}_sample_cases[0]["id"])\n',
                id=f'sampled-{category}-run')])
    notebook.cells.extend([
        nbformat.v4.new_markdown_cell('## Full evaluation and results\n\n'
            'Run this cell after defining all banks. It includes the 68 relocated cases and all '
            '760 additional cases. Running a family cell earlier is optional; completed stages are reused. '
            'A fresh independent notebook run need not execute the historical manual-suite cells.', id='full-evaluation-heading'),
        nbformat.v4.new_code_cell('# | notest\n'
            'ALL_EVALUATION_CASES = GENERAL_OPERATION_CASES + ARC_WELDING_CASES + SAMPLED_CASES\n'
            'assert len(ALL_EVALUATION_CASES) == len({c["id"] for c in ALL_EVALUATION_CASES}) == 828\n'
            'await query_embeddings.prepare([case["question"] for case in ALL_EVALUATION_CASES])\n'
            'evaluation_metrics = await evaluation.run(ALL_EVALUATION_CASES, concurrency=EVALUATION_CONCURRENCY)\n'
            'display(grouped_metrics(evaluation_metrics, ["category"]))\n'
            'display(grouped_metrics(evaluation_metrics, ["source", "revision"]))\n'
            'display(grouped_metrics(evaluation_metrics, ["kind"]))\n', id='full-evaluation-run'),
        nbformat.v4.new_markdown_cell('## Algorithm and document review\n\n'
            'The executed review will be published here. Until then, no conclusions about answer '
            'correctness are implied by retrieval diagnostics or code-example status.', id='evaluation-review'),
        nbformat.v4.new_markdown_cell('## Cleanup\n\nRun after all selected suites or interruption.', id='evaluation-cleanup-heading'),
        nbformat.v4.new_code_cell('# | notest\ntry:\n    if "app" in globals(): app.close()\nfinally:\n'
            '    for client_name in ("embedding_client", "generation_client"):\n'
            '        if client_name in globals(): await globals()[client_name]._client.aclose()\n', id='evaluation-cleanup')])
    nbformat.validate(notebook)
    atomic_json(NOTEBOOK, json.loads(nbformat.writes(notebook)))
    print('Built', len(groups), 'families', sum(map(len, groups.values())), 'new cases')


def namespace():
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    ns = {}
    for cell_id in ['evaluation-config', 'evaluation-imports', 'evaluation-helpers', 'general-operation-prompt-bank',
                    'arc-welding-prompt-bank', 'sampled-banks-start']:
        exec(next(c.source for c in notebook.cells if c.id == cell_id), ns)
    for cell in notebook.cells:
        if cell.id.startswith('sampled-') and cell.id.endswith('-bank'):
            exec(cell.source, ns)
    return notebook, ns


async def run(concurrency, prepare_only=False, skip_prepare=False):
    notebook, ns = namespace()
    setup = next(c.source for c in notebook.cells if c.id == 'evaluation-services')
    exec(setup, ns)
    cases = ns['GENERAL_OPERATION_CASES']+ns['ARC_WELDING_CASES']+ns['SAMPLED_CASES']
    cache = QueryEmbeddingCache(ns['embedding_client'], ns['app'].embed_model,
        ns['app'].embedding_config, ns['app'].dimension, ROOT/'db/object_rag_tests/query_embeddings')
    ns['app'].embedder = cache
    cache_readonly_snapshots(ns['app'])
    evaluation = EvaluationRun(ns['app'], cases, ROOT/'db/object_rag_tests/results', settings=dict(
        category_boost=ns['CATEGORY_SCORE_BOOST'], rag_host=ns['RAG_HOST'],
        rerank_source=next(c.source for c in notebook.cells if c.id == 'evaluation-config')))
    print('RUN', evaluation.run_id, evaluation.directory, flush=True)
    atomic_json(ROOT/'db/object_rag_tests/latest.json', dict(directory=str(evaluation.directory),
        run_id=evaluation.run_id, case_bank=str(evaluation.case_bank_path),
        case_bank_digest=evaluation.case_bank_digest))
    try:
        if prepare_only or not skip_prepare:
            await cache.prepare([case['question'] for case in cases])
        if prepare_only:
            return
        metrics = await evaluation.run(concurrency=concurrency)
        metrics.to_csv(evaluation.directory/'metrics.csv', index=False)
        print(metrics[['retrieval_complete', 'corpus_available', 'expected_manual_hit',
                       'expected_source_in_context', 'answer_complete']].sum().to_string(), flush=True)
    finally:
        ns['app'].close()
        await ns['embedding_client']._client.aclose()
        await ns['generation_client']._client.aclose()


def markdown_table(frame):
    columns = list(frame.columns)
    rows = ['| '+' | '.join(columns)+' |', '| '+' | '.join(['---']*len(columns))+' |']
    rows.extend('| '+' | '.join(str(v).replace('|', '\\|') for v in row)+' |'
                for row in frame.itertuples(index=False, name=None))
    return '\n'.join(rows)


def historical_review(notebook, cases):
    """Evidence-backed observations about preserved outputs, never fresh scores."""
    legacy = {c['id']: c for c in cases}
    outputs = next(c for c in notebook.cells if c.id == 'general-operation-answers').outputs
    answers, case_id = {}, None
    for output in outputs:
        text = output.get('data', {}).get('text/markdown', '')
        if text.startswith('## ['):
            case_id = text.splitlines()[0].split('] ', 1)[-1]
        elif text.startswith('### Coding or diagnostic answer') and case_id:
            answers[case_id] = text.split('\n\n', 1)[-1]
    paragraphs = [
        '### Review of preserved historical results\n\n'
        f'The relocated outputs contain {len(answers)} saved general-operation answers; '
        'the arc-welding answer cell has no saved outputs. These observations describe '
        '**historical results only**, and are not accuracy scores for the new 828-case evaluation.'
    ]
    for case_id in ['general_50_alarm_1507', 'general_50_alarm_6035', 'general_50_alarm_1569']:
        if case_id not in answers:
            continue
        case = legacy[case_id]
        excerpt = case['references'][0]['excerpt']
        answer = answers[case_id]
        if case_id.endswith('1569'):
            finding = ('The saved answer is only a manual-identification fragment and omits the '
                       'alarm meaning, cause and remedy. A nonempty explanation or accepted JSON '
                       'therefore does not establish answer completeness. The actual cause of the '
                       'fragment cannot be established from saved outputs alone.')
        else:
            finding = ('The saved answer explicitly reports that the requested code is absent '
                       'from its supplied evidence, while the source reference below contains it. '
                       'This is a retrieval/evidence-coverage failure with an appropriately cautious '
                       'answer, not evidence that the source manual lacks the code.')
        paragraphs.append(f'**`{case_id}`** — {finding}\n\n'
                          f'Source row: `{excerpt}`\n\nSaved answer: {answer}')
    paragraphs.append(
        '**Revision fidelity:** `general_40_9_1_1_wait_input` has saved summaries from both '
        'B-5 and C-1, whose WAIT examples use different input syntax. Mixed retrieval is expected '
        'under the current algorithm; source-specific context and citation checks are needed '
        'before judging whether an answer mixed syntax. The presence of both revisions alone '
        'does not prove that the answer is wrong.\n\n'
        '**Stored summaries:** the saved alarm summaries include a long synthesized troubleshooting '
        'guide, whereas the exact source row for 6035 has blank cause/remedy cells. Reviewers must '
        'distinguish generated summaries from exact evidence and preserve blanks as unknowns.'
    )
    return '\n\n'.join(paragraphs)


def publish():
    """Publish honest coverage/results snapshots without calling a model."""
    import sqlite3
    import pandas as pd
    from ribosome.rag.object import ObjectRAG, load_mapping
    notebook, ns = namespace()
    cases = ns['GENERAL_OPERATION_CASES']+ns['ARC_WELDING_CASES']+ns['SAMPLED_CASES']
    # Read-only coverage preflight uses the same mapping/snapshot code as production.
    app = object.__new__(ObjectRAG)
    app.categories, app.mapping = load_mapping(ns['PROJ_ROOT']/'assets/coding_categories')
    app.connection = sqlite3.connect((ns['INDEX_DIR']/'catalog.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    try:
        docs = app.snapshot(next(iter(app.categories)), all_categories=True)[1]
        available = {d['key'] for d in docs.values()}
        for name, value in dict(embed_model=ns['EMBED_MODEL'],
                embedding_config=dict(provider='ollama-pool', hosts=ns['HOSTS']),
                rag_model=ns['RAG_MODEL'], max_context_chars=ns['MAX_CONTEXT_CHARS'],
                max_output_tokens=ns['MAX_OUTPUT_TOKENS']).items():
            setattr(app, name, value)
        compatible = EvaluationRun(app, cases, ROOT/'db/object_rag_tests/results', settings=dict(
            category_boost=ns['CATEGORY_SCORE_BOOST'], rag_host=ns['RAG_HOST'],
            rerank_source=next(c.source for c in notebook.cells if c.id == 'evaluation-config')))
    finally:
        app.close()
    latest = read_json(ROOT/'db/object_rag_tests/latest.json', {})
    if latest.get('run_id') != compatible.run_id:
        latest = {}
    states = {}
    if latest:
        directory = Path(latest['directory'])
        for case in cases:
            state = read_json(directory/(case['id']+'-'+digest(case)[:12]+'.json'), {})
            if state.get('case_digest') == digest(case) and state.get('run_id') == latest['run_id']:
                states[case['id']] = state
    frame = pd.DataFrame([case_metrics(c, states.get(c['id'], {}), available) for c in cases])
    previous_fresh_outputs = bool(notebook.metadata.get('object_rag_evaluation')) or any(
        c.get('outputs') and (c.id in ('recorded-legacy-results', 'full-evaluation-run') or
                             c.id.startswith('sampled-') and c.id.endswith('-run')) for c in notebook.cells)
    if not states and previous_fresh_outputs:
        raise FileNotFoundError('No compatible saved results; the existing notebook and outputs were preserved')
    sampled = frame[frame.case_id.str.startswith('sample_')]
    files = sampled[['source', 'revision', 'corpus_available']].drop_duplicates()
    missing = files[~files.corpus_available]
    completed = int(frame.answer_complete.sum())
    report = (
        '## Algorithm and document review\n\n'
        f'**Fresh evaluation status:** {int(frame.retrieval_complete.sum())}/828 retrievals and '
        f'{completed}/828 answers completed in the published result snapshot. '
        +('No compatible fresh model results are recorded yet. '
          'Retrieval accuracy, answer accuracy, and causal claims about ranking changes cannot '
          'be inferred from the coverage preflight.\n\n' if not states else
          f'Run identity: `{latest["run_id"]}`. Result files are in `{latest["directory"]}`.\n\n')+
        '### Corpus availability\n\n'
        f'The mapped active index contains {len(docs)} corpus documents representing '
        f'**{int(files.corpus_available.sum())}/38 CSV sources**. '
        f'Of the new cases, **{int(sampled.corpus_available.sum())}/760** have their expected source '
        f'indexed; **{len(sampled)-int(sampled.corpus_available.sum())}/760** target absent revisions. '
        f'Including the 68 legacy cases, the indexed-source denominator is '
        f'**{int(frame.corpus_available.sum())}/828**. Corpus 4.0/5.0 profiles are not the '
        'manual V3.0/V4.0/V5.0 revision labels.\n\n'
        +markdown_table(missing[['source', 'revision']])+'\n\n'
        +('### Fresh run metrics\n\n'+markdown_table(grouped_metrics(frame, ['category']))+'\n\n'
          'Overall and indexed-source-only counts:\n\n'+markdown_table(pd.DataFrame([
              dict(denominator='all cases', cases=len(frame),
                   source_in_context=int(frame.expected_source_in_context.sum()), answers=int(frame.answer_complete.sum())),
              dict(denominator='indexed sources', cases=int(frame.corpus_available.sum()),
                   source_in_context=int(frame.loc[frame.corpus_available, 'expected_source_in_context'].sum()),
                   answers=int(frame.loc[frame.corpus_available, 'answer_complete'].sum()))]))+'\n\n' if states else '')
        +historical_review(notebook, cases)+'\n\n'
        '### Priorities supported by the evidence\n\n'
        '1. **Pack exact alarm rows:** fresh 1507 and 6035 results contain the codes in matching '
        'retrieved objects, but lose them when large tables exceed the context budget. Select '
        'the specific cited child rows before packing parent tables; lexical/code lookup can '
        'help locate those rows. Increasing hit counts alone does not fix this failure.\n'
        '2. **Answer completeness:** add a task-appropriate fact checklist to evaluation. '
        'The fresh 1569 and 20081 fragments show why JSON validity and a nonempty answer are insufficient. '
        'Procedural and alarm answers should be assessed without requiring a code example.\n'
        '3. **Revision-aware evidence:** expose expected source identity separately from RC profile. '
        'Retain the mixed-revision baseline while measuring which cited blocks actually reached generation.\n'
        '4. **Corpus and representation coverage:** the 15 absent CSV revisions are ingestion gaps, '
        'not ranking failures. Production selects tables and outer table rows; the load-analysis '
        'reference tasks are prose and screenshot-adjacent. The full run is needed to measure '
        'the effect of that eligibility rule. No image content has been inferred or OCR added.\n'
        '5. **Document quality:** keep missing alarm cause/remedy cells explicit (6035), normalize '
        'heading paths across renumbered revisions, and provide text equivalents for screenshot-only '
        'UI fields. These changes would improve traceability; their retrieval benefit remains unmeasured.\n\n'
        '### Assessment method and limits\n\n'
        'Canonical source identity, heading matches and anchors are retrieval diagnostics only. '
        'Expected facts stay outside generation context. Answer assessments must compare completeness, '
        'citation support, revision fidelity and unsupported claims against exact source excerpts. '
        'No unreviewed answer is counted as correct. Historical observations above are separately '
        'identified; no correctness percentage is claimed for unexecuted cases. '
        'Questions include manual revisions and source-derived terms, and supply the category '
        'explicitly. These tests do not measure automatic category classification or the full '
        'range of unconstrained user queries.'
        ' No category-boost ablation was run, so this baseline does not estimate the causal '
        'benefit of the 0.15 boost. Source grounding was reviewed; controller programs were not executed.'
    )
    reviewed = [compatible_review(c, states.get(c['id'], {})) for c in cases]
    reviewed = [r for r in reviewed if r.get('status') == 'complete']
    if reviewed:
        review_frame = pd.DataFrame(reviewed)
        review_counts = review_frame.groupby(['method', 'assessment', 'completeness']).size().reset_index(name='cases')
        report += ('\n\n### Fresh model-assisted source assessments\n\n'
            f'**{len(reviewed)}/828** cases have a completed review or execution-diagnostic record. The judge uses the configured '
            'model separately from production generation, with source-text renderings of the stored '
            'exact references and expected facts '
            'that are never added to answer prompts. These are model-assisted assessments, '
            '**not independently verified accuracy measurements**.\n\n'+markdown_table(review_counts)+'\n\n')
        review_plan = read_json(Path(latest['directory'])/'review-plan.json', {})
        if review_plan:
            report += (f'The qualitative review plan covers **{review_plan["reviewed_cases"]}/828** cases: '
                       +review_plan['selection']+'. All 828 answers and retrieval records remain '
                       'available for inspection; unreviewed cases receive no correctness grade.\n\n')
        selected = []
        for label in ['incorrect', 'partial', 'abstained', 'correct', 'not_assessable']:
            selected.extend([c for c in cases if compatible_review(c, states.get(c['id'], {})).get('assessment') == label][:2])
        for case in selected:
            assessment = compatible_review(case, states[case['id']])
            report += (f'**`{case["id"]}` — {assessment["assessment"]}.** '
                +assessment['rationale']+'\n\nReviewer-view excerpt (HTML formatting removed): `'+assessment['evidence_quote'].replace('`', "'")+'`\n\n')
        warnings = sum(bool(r.get('validation_warnings')) for r in reviewed)
        report += (f'{warnings} judge responses needed nonliteral quotations omitted. Raw judge '
                   'responses and warnings remain available in the review records; an omitted '
                   'quotation is never presented as an exact source quote.\n\n')
    if states:
        indexed = frame[frame.corpus_available & frame.retrieval_complete]
        anchored = indexed[indexed.anchors_in_matching_hits.notna()]
        anchor_hits = anchored[anchored.anchors_in_matching_hits.eq(True)]
        diagnostics = pd.DataFrame([
            dict(diagnostic='Expected CSV/legacy source absent', cases=int((~frame.corpus_available).sum()), denominator=len(frame)),
            dict(diagnostic='Expected source not retrieved (indexed cases)', cases=int((~indexed.expected_manual_hit).sum()), denominator=len(indexed)),
            dict(diagnostic='Expected anchors absent from matching hits', cases=int(anchored.anchors_in_matching_hits.eq(False).sum()), denominator=len(anchored)),
            dict(diagnostic='Anchors found in hits but lost from context', cases=int(anchor_hits.anchors_in_context.eq(False).sum()), denominator=len(anchor_hits)),
            dict(diagnostic='Context has budget omissions', cases=int(frame.omissions.gt(0).sum()), denominator=len(frame)),
            dict(diagnostic='Retrieval exceptions', cases=int(frame.retrieval_error.ne('').sum()), denominator=len(frame)),
            dict(diagnostic='Generation exceptions or invalid responses', cases=int(frame.answer_error.ne('').sum()), denominator=int(frame.retrieval_complete.sum())),
        ])
        report += ('\n\n### Failure-stage diagnostics\n\n'+markdown_table(diagnostics)+
                   '\n\nCounts can overlap. Header-only citation blocks do not count as supplied '
                   'source evidence. Anchor matches are diagnostics, not answer-correctness labels.\n')
        audited = read_json(Path(latest['directory'])/'source-audits.json', [])
        bank = {c['id']: c for c in cases}
        valid_audits = [a for a in audited if a['case_id'] in states
            and a.get('case_digest') == digest(bank[a['case_id']])
            and a.get('retrieval_digest') == digest(states[a['case_id']].get('retrieval', {}))
            and a.get('answer_digest') == digest(states[a['case_id']].get('answer', {}))]
        if valid_audits:
            report += '\n\n### Source-audited fresh failures\n\n'
            for audit in valid_audits:
                report += (f'**`{audit["case_id"]}` — {audit["stage"]}.** '+audit['finding']+
                    '\n\nExact reference row: `'+audit['reference']+'`\n\nRecorded answer: '+audit['answer']+'\n\n')
            report += ('The 1507/6035 examples show why increasing hit counts alone is insufficient: '
                'the relevant identifiers were already in retrieved objects. Select or extract the '
                'specific cited child rows before packing large tables. The 1569/20081 examples '
                'separately demonstrate answer-completeness failures with facts already supplied. '
                'Collect finish reasons and token usage before attributing fragments to any one '
                'model or budget cause.\n')
    profile = read_json(ROOT/'db/object_rag_tests/catalog-profile.json')
    if profile and profile.get('run_id') == compatible.run_id:
        report += ('\n\n### Local retrieval timing sample\n\n'
            f'One warm-query sample over {profile["objects"]:,} active objects took '
            f'{profile["snapshot_seconds"]:.3f}s to load the catalog snapshot and '
            f'{profile["retrieval_seconds"]:.3f}s for retrieval with a cached query vector. '
            f'Reusing that snapshot took {profile["cached_snapshot_retrieval_seconds"]:.3f}s '
            'and returned identical evidence in this comparison. These timings exclude query '
            'embedding and answer generation and are not corpus-wide latency estimates. '
            'The production method scans and decodes the active catalog on each request; '
            'a snapshot cache keyed by active generation identities is a concrete optimization '
            'to benchmark separately before changing the retriever. The evaluation runner now '
            'memoizes these read-only views and checks generation/settings identities on each '
            'use; eligibility, ordering, ranking and evidence assembly remain the production methods.\n')
    types = read_json(ROOT/'db/object_rag_tests/catalog-type-coverage.json', {})
    if types.get('run_id') == compatible.run_id:
        report += ('\n\n### Indexed content and retrieval eligibility\n\n'
            +markdown_table(pd.DataFrame(types['rows']))+'\n\n'
            'The production retriever admits tables and outer table rows. It does not select '
            'list, list-item, figure, row-group or cell vectors as independent candidates. '
            'In particular, **10,689 list-item vectors already exist** and provide a concrete '
            'route to testing broader procedural coverage without regenerating completed summaries.\n\n')
        example = read_json(ROOT/'db/object_rag_tests/excluded-procedure-example.json', {})
        if example.get('run_id') == compatible.run_id:
            report += (f'For example, indexed `{example["type"]}` object `{example["object_id"]}` '
                'contains this load-analysis instruction, but that object type is not an eligible '
                'candidate:\n\n> '+example['content'].strip().replace('\n','\n> ')+'\n\n'
                'Related tables can still be returned. Finding the correct manual therefore '
                'does not establish that the requested procedural fact reached the model; '
                'the supplied-context and source-review checks address that distinction.\n')
    next(c for c in notebook.cells if c.id == 'evaluation-review').source = report
    if latest:
        snapshot = json.loads(nbformat.writes(nbformat.read(NOTEBOOK, as_version=4)))
        atomic_json(Path(latest['directory'])/'notebook-snapshots'/(digest(snapshot)[:16]+'.ipynb'), snapshot)
        notebook.metadata['object_rag_evaluation'] = dict(run_id=latest['run_id'], total_cases=len(cases),
            completed_retrievals=int(frame.retrieval_complete.sum()), completed_answers=completed)
    # Publish the exact run's answers/summaries as saved notebook outputs, while
    # preserving the moved historical cells and their outputs without alteration.
    for category in sorted({c['category'] for c in cases if c['id'].startswith('sample_')}):
        cell = next(c for c in notebook.cells if c.id == f'sampled-{category}-run')
        selected = [c for c in cases if c['id'].startswith('sample_') and c['category'] == category]
        completed_cases = [c for c in selected if states.get(c['id'])]
        cell.outputs = []  # Never leave another run's outputs under the current report.
        if completed_cases:
            table = frame[frame.case_id.isin([c['id'] for c in selected])]
            cell.outputs = [nbformat.v4.new_output('display_data', data={
                'text/plain': table.to_string(index=False), 'text/html': table.to_html(index=False)})]
            cell.outputs += [nbformat.v4.new_output('display_data', data={'text/markdown': text})
                for case in completed_cases for text in answer_blocks(case, states[case['id']])]
    legacy_id = 'recorded-legacy-results'
    if states:
        if not any(c.id == legacy_id for c in notebook.cells):
            position = next(i for i,c in enumerate(notebook.cells) if c.id == 'evaluation-review')
            notebook.cells.insert(position, nbformat.v4.new_code_cell(
                '# | notest\n# Fresh saved results, separate from historical outputs above.\n'
                'evaluation.show_answers(GENERAL_OPERATION_CASES + ARC_WELDING_CASES)\n', id=legacy_id))
        cell = next(c for c in notebook.cells if c.id == legacy_id)
        cell.outputs = [nbformat.v4.new_output('display_data', data={'text/markdown': text})
            for case in cases if not case['id'].startswith('sample_') and states.get(case['id'])
            for text in answer_blocks(case, states[case['id']])]
        cell = next(c for c in notebook.cells if c.id == 'full-evaluation-run')
        cell.outputs = [nbformat.v4.new_output('display_data', data={
            'text/plain': grouped_metrics(frame, columns).to_string(index=False),
            'text/html': grouped_metrics(frame, columns).to_html(index=False)})
            for columns in [['category'], ['source', 'revision'], ['kind']]]
    nbformat.validate(notebook)
    atomic_json(NOTEBOOK, json.loads(nbformat.writes(notebook)))
    target = ROOT/'db/object_rag_tests'
    target.mkdir(parents=True, exist_ok=True)
    atomic_text(target/'published-metrics.csv', frame.to_csv(index=False))
    if latest:
        atomic_text(Path(latest['directory'])/'metrics.csv', frame.to_csv(index=False))
    atomic_text(target/'review.md', report)
    print('Published', len(cases), 'cases;', completed, 'fresh completed answers;',
          int(files.corpus_available.sum()), '/38 CSV files indexed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['author-offline', 'build', 'prepare', 'run', 'publish'])
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--skip-prepare', action='store_true',
                        help='Consume a cache being prepared separately; embed individual misses on demand')
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error('concurrency must be positive')
    if args.stage == 'build':
        build()
    elif args.stage == 'author-offline':
        offline_author()
    elif args.stage == 'publish':
        publish()
    else:
        asyncio.run(run(args.concurrency, prepare_only=args.stage == 'prepare', skip_prepare=args.skip_prepare))
