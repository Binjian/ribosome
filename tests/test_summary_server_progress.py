"""Progress colors reflect current routing, including multi-server documents."""
from types import SimpleNamespace

from utils.summary_asyncio import RequestMetrics
from utils.summary_object_asyncio import _server_progress


def test_server_colors_follow_active_document_requests():
    first, second = RequestMetrics(), RequestMetrics()
    client = SimpleNamespace(hosts=['http://one', 'http://two'], server_metrics=[first, second])
    first.start('a.md')
    second.start('a.md')
    second.start('a.md')
    first.start('b.md')
    text, color = _server_progress(client, 'a.md')
    assert '🔵 S1 http://one: 1' in text
    assert '🟠 S2 http://two: 2' in text
    assert color == '#ea580c'
    text, color = _server_progress(client, 'b.md')
    assert text == '🔵 S1 http://one: 1'
    assert color == '#2563eb'
    assert _server_progress(client, 'idle.md') == ('', None)
    text, _ = _server_progress(client)
    assert 'S1 http://one: 2' in text and 'S2 http://two: 2' in text
    assert _server_progress(object(), 'a.md') == ('', None)
