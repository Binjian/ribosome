"""Offline checks for category-only evaluation inputs and prompt isolation."""
import ast
import asyncio
from pathlib import Path
import re
import sys

import nbformat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'utils'))
from object_rag_category_questions import (
    CATEGORY_SYSTEM_PROMPT, CategoryPromptGenerator, category_question,
)


def banks(notebook):
    ns = {}
    for cell in notebook.cells:
        if (cell.id.endswith('prompt-bank') or cell.id == 'sampled-banks-start'
                or cell.id.startswith('sampled-') and cell.id.endswith('-bank')):
            exec(cell.source, ns)
    return ns['GENERAL_OPERATION_CASES'] + ns['ARC_WELDING_CASES'] + ns['SAMPLED_CASES']


def test_all_questions_are_rewritten_and_references_preserved():
    baseline = nbformat.read('nbs/06.test.rag.object.question_with_location.ipynb', 4)
    variant = nbformat.read('nbs/06.test.rag.object.question_with_category.ipynb', 4)
    nbformat.validate(variant)
    originals, cases = banks(baseline), banks(variant)
    assert len(cases) == len({c['id'] for c in cases}) == 828
    for original, case in zip(originals, cases):
        assert case['question'] == category_question(original['question'])
        assert case['question'] != original['question']
        assert {k: v for k, v in case.items() if k != 'question'} == {
            k: v for k, v in original.items() if k != 'question'}
        assert not re.search(r'请依据|根据《|根据手册|第[\d.]+[章节]|SX322|V[345]\.0|[（(][ABC]-\d', case['question'])
    for cell in variant.cells:
        if cell.cell_type == 'code':
            compile(cell.source, str(Path('category_questions.ipynb')), 'exec',
                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    assert 'object_rag_evaluation' not in variant.metadata
    services = next(c.source for c in variant.cells if c.id == 'evaluation-services')
    assert 'CategoryPromptGenerator(generation_client)' in services
    imports = next(c.source for c in variant.cells if c.id == 'evaluation-imports')
    assert 'CategoryEvaluationRun as EvaluationRun' in imports
    assert 'db/object_rag_tests/question_with_category/results' in services
    assert 'category_system_prompt=CATEGORY_SYSTEM_PROMPT' in services


def test_keep_task_identity_without_section_address():
    assert category_question('根据手册“手册.md”（V5.0 / C-1），要在“1.1 基础配置”中配置“总线IO映射”，应按什么菜单路径进入？') == '要配置“总线IO映射”，应按什么菜单路径进入？'
    assert category_question('根据《手册》（V5.0 / C-1），“MOVL”中“参数2”的功能是什么？') == '关于MOVL，“参数2”的功能是什么？'


def test_category_prompt_is_per_request_and_does_not_change_original_messages():
    class FakeGenerator:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            return {'message': {'content': '{}'}}

    async def run():
        fake = FakeGenerator()
        generator = CategoryPromptGenerator(fake)
        for category in ['general_operation', 'arc_welding']:
            messages = [dict(role='system', content='Production rules'),
                        dict(role='user', content=f'Selected category: {category}\nQuestion: MOVL?\nRetrieved manual evidence:\n[E1] content')]
            await generator.chat(messages=messages, model='test', options={'temperature': 0})
            sent = fake.calls[-1]
            assert sent['messages'][0]['content'] == 'Production rules\n' + CATEGORY_SYSTEM_PROMPT.format(category=category)
            assert sent['messages'][1:] == messages[1:]
            assert messages[0]['content'] == 'Production rules'
            assert sent['model'] == 'test'
    asyncio.run(run())


def test_locations_and_images_use_actual_hits_not_gold_references(tmp_path):
    from copy import deepcopy
    from object_rag_category_questions import evidence_locations, located_answer_blocks
    root = tmp_path
    source = root / 'assets/manuals/Manual (C-1).md'
    source.parent.mkdir(parents=True)
    image = source.parent / 'img/screen (1).png'
    image.parent.mkdir()
    image.write_bytes(b'image')
    source.write_text('# 1 Setup\n## 1.2 Settings\nSee figure 1\n![Setup](<img/screen (1).png>)\n## 1.3 Other\n![Other](img/no.png)\n')
    hit = dict(record=dict(id='row-2', source_file=str(source),
                          section_headings=['1 Setup', '1.2 Settings'],
                          attributes=dict(line_start=3, line_end=3), content='See figure 1'),
               manual=dict(profile='5.0', revision='V5.0 / C-1'))
    state = dict(retrieval=dict(hits=[hit], context='[E1] manual | source | lines 3-3 | row-2\nSee figure 1'),
                 answer=dict(answer='Use the settings.', retrieved_summary='Settings'))
    before = deepcopy(state)
    rendered = evidence_locations(state, root)
    assert '**E1 — 1 Setup → 1.2 Settings**' in rendered
    assert 'lines 3–3' in rendered and 'V5.0 / C-1' in rendered
    assert '#L3-L3' in rendered
    assert '![Setup](<assets/manuals/img/screen%20%281%29.png>)' in rendered
    assert 'no.png' not in rendered and str(root) not in rendered
    case = dict(id='test', question='Settings?', references=[{'excerpt': 'SECRET GOLD'}])
    answer = located_answer_blocks(case, state, root)[2]
    assert 'Use the settings.' in answer and '1.2 Settings' in answer
    assert 'SECRET GOLD' not in answer
    assert state == before


def test_omitted_hits_have_no_fabricated_evidence_label_or_image(tmp_path):
    from object_rag_category_questions import evidence_locations
    source = tmp_path / 'assets/manual.md'
    source.parent.mkdir()
    source.write_text('# Setup\n')
    image = source.parent / 'img/real.png'
    image.parent.mkdir()
    image.write_bytes(b'image')
    hit = dict(record=dict(id='omitted', source_file=str(source), section_headings=[],
                          attributes=dict(line_start=1, line_end=1),
                          content='<img src="img/real.png"/><img src="img/missing.png"/><img src="../../outside.png"/>'), manual={})
    rendered = evidence_locations(dict(retrieval=dict(hits=[hit], context='')), tmp_path)
    assert 'Hit 1 — Subsection unavailable' in rendered
    assert 'not supplied to generation' in rendered and '**E1' not in rendered
    assert '![Source image](<assets/img/real.png>)' in rendered
    assert 'Image file unavailable: `assets/img/missing.png`' in rendered
    assert 'outside' not in rendered


def test_actual_context_labels_survive_budget_omissions(tmp_path):
    from object_rag_category_questions import evidence_locations
    hits = [dict(record=dict(id=i, source_file='assets/manual.md', attributes={},
                             content='', section_headings=['2 Commands']), manual={})
            for i in ['first-omitted', 'second-included']]
    rendered = evidence_locations(dict(retrieval=dict(hits=hits,
        context='[E1] manual | source | lines 3-3 | second-included\nCode')), tmp_path)
    assert '**Hit 1 —' in rendered and '**E1 —' in rendered and '**E2 —' not in rendered


def test_converted_markdown_links_back_to_original_document(tmp_path):
    from object_rag_category_questions import evidence_locations
    source = tmp_path / 'assets/code-docs.5.0/.md/manuals/Manual/Manual.md'
    source.parent.mkdir(parents=True)
    source.write_text('# 3.2 Configuration\n')
    original = tmp_path / 'assets/code-docs.5.0/manuals/Manual.docx'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'docx')
    state = dict(retrieval=dict(hits=[dict(record=dict(id='r', source_file=str(source),
        attributes={}, content='', section_headings=['3.2 Configuration']), manual={})], context=''))
    assert 'original document [Manual.docx](<assets/code-docs.5.0/manuals/Manual.docx>)' in evidence_locations(state, tmp_path)


def test_jupyter_links_resolve_from_notebook_directory(tmp_path):
    from urllib.parse import unquote
    from object_rag_category_questions import evidence_locations
    notebook_directory = tmp_path / 'nbs'
    notebook_directory.mkdir()
    source = tmp_path / 'assets/manuals/Manual (A-1).md'
    source.parent.mkdir(parents=True)
    source.write_text('# 1 Setup\n')
    image = source.parent / 'img/图 (1).png'
    image.parent.mkdir()
    image.write_bytes(b'image')
    hit = dict(record=dict(id='r', source_file=str(source), section_headings=['1 Setup'],
        attributes={}, content='<img src="img/图 (1).png"/>'), manual={})
    text = evidence_locations(dict(retrieval=dict(hits=[hit], context='')), tmp_path,
                              display_directory=notebook_directory)
    assert '](<../assets/' in text
    targets = re.findall(r'\]\(<([^>]+)>\)', text)
    assert len(targets) == 2
    assert {(notebook_directory / unquote(t)).resolve() for t in targets} == {source, image}
