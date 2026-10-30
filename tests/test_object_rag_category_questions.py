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
            assert not cell.outputs and cell.execution_count is None
    assert 'object_rag_evaluation' not in variant.metadata
    services = next(c.source for c in variant.cells if c.id == 'evaluation-services')
    assert 'CategoryPromptGenerator(generation_client)' in services
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
