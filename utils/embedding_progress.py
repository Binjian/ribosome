"""Live notebook display for asynchronous embedding requests; no provider calls."""

from html import escape
from time import perf_counter


def render_embedding_status(snapshot, *, current_file, elapsed, file_elapsed,
                            finished, total, state, completed_files, document_counts):
    rows = []
    for index, server in enumerate(snapshot['servers']):
        colour = ('#2563eb', '#ea580c', '#16a34a', '#9333ea')[index % 4]
        active, capacity = server['active'], server['capacity']
        width = min(100, 100 * active / max(1, capacity))
        rows.append(
            f'<tr><td style="padding:5px;color:{colour}"><strong>S{index + 1}</strong> '
            f'{escape(server["host"])}</td>'
            f'<td style="padding:5px">{active}/{capacity}'
            '<div style="width:120px;height:7px;background:#ddd;border-radius:4px">'
            f'<div style="width:{width:.1f}%;height:7px;background:{colour};border-radius:4px"></div></div></td>'
            f'<td style="padding:5px">{server["completed"]}</td>'
            f'<td style="padding:5px">{server["failed"]}</td>'
            f'<td style="padding:5px">{server["cancelled"]}</td>'
            f'<td style="padding:5px">{server["oldest_seconds"]:.1f}s</td></tr>')
    files = '\n'.join(completed_files) or 'No completed files yet.'
    return (
        '<div style="padding:10px;border:1px solid #aaa;border-radius:6px">'
        f'<strong>Embedding status: {escape(state)}</strong> · Documents {finished}/{total} '
        f'· Batch {elapsed:.1f}s<br>'
        f'Indexed {document_counts["indexed"]} · Unchanged {document_counts["unchanged"]} '
        f'· Document failures {document_counts["failed"]}<br>'
        f'<strong>Requests running {snapshot["running"]}/{snapshot["capacity"]}</strong> '
        f'· Queued {snapshot["queued"]} · Completed {snapshot["completed"]} '
        f'· Failed {snapshot["failed"]} · Cancelled {snapshot["cancelled"]} '
        f'· Oldest {snapshot["oldest_seconds"]:.1f}s<br>'
        f'<div style="overflow-wrap:anywhere"><strong>Current file ({file_elapsed:.1f}s):</strong> '
        f'{escape(current_file or "Waiting for first file")}</div>'
        '<table style="text-align:left;margin-top:6px"><thead><tr>'
        '<th>Ollama server</th><th>Running / limit</th><th>Completed</th>'
        '<th>Failed</th><th>Cancelled</th><th>Oldest running</th>'
        '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'
        f'<details><summary>Completed files this run ({len(completed_files)})</summary>'
        f'<pre style="white-space:pre-wrap">{escape(files)}</pre></details></div>')


class EmbeddingProgressDisplay:
    """Update one HTML display in place; request counters come from the pool."""

    def __init__(self, pool, *, total, display_fn=None, clock=perf_counter):
        from IPython.display import HTML, display
        self.pool, self.total = pool, total
        self.clock = clock
        self.started = clock()
        self.file_started = self.started
        self.current_file = ''
        self.handle = None
        self._HTML = HTML
        self._display = display_fn or display

    def update(self, *, current_file='', report=(), state='Running'):
        now = self.clock()
        current_file = str(current_file)
        if current_file != self.current_file:
            self.current_file = current_file
            self.file_started = now
        completed = [str(row.semantics_file) for row in report if row.status in {'indexed', 'unchanged'}]
        counts = {status: sum(row.status == status for row in report)
                  for status in ('indexed', 'unchanged', 'failed')}
        content = self._HTML(render_embedding_status(
            self.pool.status_snapshot(), current_file=self.current_file,
            elapsed=now - self.started, file_elapsed=now - self.file_started,
            finished=len(report), total=self.total, state=state, completed_files=completed,
            document_counts=counts))
        if self.handle is None:
            self.handle = self._display(content, display_id=True)
        else:
            self.handle.update(content)
