"""The extracted utilities work independently of the DOM summarization runtime."""
import asyncio
import json
import subprocess
import sys

from ribosome.core.dom.summary import object_utils


def test_utilities_import_without_loading_the_dom_summarizer():
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; from ribosome.core.dom.summary import object_utils; '
         'assert "ribosome.core.dom.summary.object" not in sys.modules; '
         'assert "ribosome.core.dom.utils" not in sys.modules'],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_row_summaries_use_the_supplied_runtime_and_provider():
    table = {'id': 1, 'source_file': 'manual.md', 'caption': 'Values',
             'content': '<table><tr><td rowspan="2">A</td><td>one</td></tr>'
                        '<tr><td>two</td></tr></table>'}
    table['children'], table['headers'] = object_utils._table_row_records(table, 1)
    requests, mapped_sizes = [], []
    async def scenario():
        semaphore = asyncio.Semaphore(1)
        async def ordered_map(function, values):
            mapped_sizes.append(len(values))
            return [await function(i, value) for i, value in enumerate(values)]
        async def summarize_response(client, content, model, role, lang):
            assert client == 'custom-client' and model == 'custom-model'
            assert role == 'user' and lang == 'en' and semaphore.locked()
            payload = json.loads(content.split('\n', 1)[1])
            requests.append(payload)
            return str(payload['row_index']) if 'row_index' in payload else '|'.join(payload['child_summaries'])
        return await object_utils._summarize_table_child_async(
            table['children'][0], table, 'custom-client', 'custom-model', 'en',
            ordered_map=ordered_map, get_semaphore=lambda: semaphore,
            summarize_response=summarize_response)
    group = asyncio.run(scenario())
    assert group['summary'] == '1|2'
    assert mapped_sizes == [2, 0, 0]
    assert [child['summary'] for child in group['children']] == ['1', '2']
    assert requests[-1]['child_summaries'] == ['1', '2']
