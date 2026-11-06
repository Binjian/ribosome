"""Execute notebook helpers against temporary files, never production corpora."""
import ast
import json
from pathlib import Path

from nbformat import read, validate
import pytest

from test_summary_task import document, section, table, wrapper


CORE_NOTEBOOK = Path('nbs/01.core.dom.summary.task.ipynb')
APPLICATION_NOTEBOOK = Path('nbs/05.application.summary.task.ipynb')


def code_with(marker):
    return next(cell.source for cell in read(APPLICATION_NOTEBOOK, as_version=4).cells
                if cell.cell_type == 'code' and marker in cell.source)


def helpers():
    namespace = {}
    exec(compile(code_with('def discover_semantics'), str(APPLICATION_NOTEBOOK), 'exec'), namespace)
    return namespace


def semantics(folder, name, tree=None):
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f'{name}_semantics.json'
    file.write_text(json.dumps(tree if tree is not None else document(wrapper(table()))), encoding='utf-8')
    return file


@pytest.mark.parametrize('path', [CORE_NOTEBOOK, APPLICATION_NOTEBOOK])
def test_notebooks_validate_compile_and_have_no_saved_execution(path):
    notebook = read(path, as_version=4)
    validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            compile(cell.source, str(path), 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            assert cell.execution_count is None and not cell.outputs


def test_configuration_resolves_root_and_honors_override(tmp_path, monkeypatch):
    source = code_with('def resolve_project_root')
    (tmp_path / 'pyproject.toml').write_text('')
    notebooks = tmp_path / 'nbs'
    notebooks.mkdir()
    monkeypatch.chdir(notebooks)
    monkeypatch.delenv('PROJ_ROOT', raising=False)
    namespace = {}
    exec(compile(source, str(APPLICATION_NOTEBOOK), 'exec'), namespace)
    assert namespace['PROJ_ROOT'] == tmp_path
    assert namespace['INPUT_DIRS'] == [tmp_path / 'assets/code-docs.4.0/.md', tmp_path / 'assets/code-docs.5.0/.md']
    assert not (tmp_path / '.env').exists()
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path / 'missing'))
    with pytest.raises(FileNotFoundError, match='Invalid PROJ_ROOT'):
        namespace['resolve_project_root']()
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    assert namespace['resolve_project_root']() == tmp_path


def test_discovery_order_nested_inputs_and_ignored_outputs(tmp_path):
    discover = helpers()['discover_semantics']
    first, second = tmp_path / 'first', tmp_path / 'second'
    a = semantics(first, 'a')
    b = semantics(first / 'nested', 'b')
    c = semantics(second, 'c')
    (first / 'ignored_tasks.json').write_text('{}')
    (first / 'ignored_summary-progress.json').write_text('{}')
    assert discover([second, first, first / 'nested', second]) == [
        (second, [c]), (first, [a, b]), (first / 'nested', []), (second, []),
    ]
    assert discover(first) == [(first, [a, b])]


@pytest.mark.parametrize('mode', ['missing', 'empty', 'no-folders'])
def test_discovery_rejects_invalid_inputs_before_writing(tmp_path, mode):
    discover = helpers()['discover_semantics']
    if mode == 'missing':
        with pytest.raises(FileNotFoundError, match='does not exist'):
            discover([tmp_path / 'missing'])
    elif mode == 'empty':
        with pytest.raises(ValueError, match='No .*semantics'):
            discover([tmp_path])
    else:
        with pytest.raises(ValueError, match='at least one'):
            discover([])
    assert not list(tmp_path.rglob('*_tasks.json'))


def test_suffix_mapping():
    output = helpers()['task_output_path']
    assert output(Path('/tmp/manual_semantics.json')) == Path('/tmp/manual_tasks.json')
    with pytest.raises(ValueError, match='Expected'):
        output('/tmp/manual_tasks.json')


def test_batch_preserves_originals_and_checkpoints_and_skips_equal_outputs(tmp_path):
    namespace = helpers()
    first = semantics(tmp_path, 'a', document(section('作业模板', [wrapper(table())])))
    second = semantics(tmp_path / 'nested', 'b')
    checkpoint = tmp_path / '.object-summary-checkpoint.json'
    checkpoint.write_text('existing checkpoint')
    original = {file: file.read_bytes() for file in (first, second, checkpoint)}
    corpora = namespace['discover_semantics']([tmp_path])
    rows = namespace['classify_task_files'](corpora, progress=False)
    assert [row['status'] for row in rows] == ['written', 'written']
    assert [row['converted_tables'] for row in rows] == [1, 0]
    assert [row['task_count'] for row in rows] == [1, 0]
    assert all(row['elapsed_seconds'] >= 0 for row in rows)
    outputs = [Path(row['output']) for row in rows]
    assert outputs == [tmp_path / 'a_tasks.json', tmp_path / 'nested/b_tasks.json']
    converted = json.loads(outputs[0].read_text())
    assert converted['summary'] == 'Existing document'
    assert converted['blocks'][0]['c'][1]['c'][0]['object']['type'] == 'task'
    assert json.loads(outputs[1].read_text()) == json.loads(second.read_text())
    prior_outputs = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in outputs}
    repeated = namespace['classify_task_files'](corpora, progress=False)
    assert [row['status'] for row in repeated] == ['unchanged', 'unchanged']
    assert {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in outputs} == prior_outputs
    assert {file: file.read_bytes() for file in original} == original
    # Changed source summaries and classifications must replace the derived output.
    updated = json.loads(first.read_text())
    updated['summary'] = 'Updated document'
    first.write_text(json.dumps(updated))
    changed = namespace['classify_task_files'](corpora, progress=False)
    assert [row['status'] for row in changed] == ['written', 'unchanged']
    assert json.loads(outputs[0].read_text())['summary'] == 'Updated document'


@pytest.mark.parametrize('invalid', ['{bad json', '[]', '{}', '{"blocks": null}'])
def test_malformed_input_preserves_previous_task_output_and_other_files_continue(tmp_path, invalid):
    namespace = helpers()
    broken = tmp_path / 'a_semantics.json'
    broken.write_text(invalid)
    old = tmp_path / 'a_tasks.json'
    old.write_text('previous task output')
    good = semantics(tmp_path, 'z')
    rows = namespace['classify_task_files'](namespace['discover_semantics'](tmp_path), progress=False)
    assert [row['status'] for row in rows] == ['failed', 'written']
    assert rows[0]['error']
    assert old.read_text() == 'previous task output'
    assert good.read_text()
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('failure', [OSError('replacement failed'), KeyboardInterrupt()])
def test_atomic_replacement_failure_or_interrupt_preserves_output_and_cleans_temp(tmp_path, monkeypatch, failure):
    namespace = helpers()
    output = tmp_path / 'manual_tasks.json'
    output.write_text('previous')
    def fail(*args):
        raise failure
    monkeypatch.setattr(namespace['os'], 'replace', fail)
    with pytest.raises(type(failure)):
        namespace['write_task_output'](output, document())
    assert output.read_text() == 'previous'
    assert set(tmp_path.iterdir()) == {output}


def test_serialization_failure_preserves_output_and_cleans_temp(tmp_path):
    namespace = helpers()
    output = tmp_path / 'manual_tasks.json'
    output.write_text('previous')
    with pytest.raises(TypeError):
        namespace['write_task_output'](output, {'bad': object()})
    assert output.read_text() == 'previous'
    assert set(tmp_path.iterdir()) == {output}


def test_failed_publish_reports_error_and_continues_batch(tmp_path, monkeypatch):
    namespace = helpers()
    first, second = semantics(tmp_path, 'a'), semantics(tmp_path, 'b')
    old = tmp_path / 'a_tasks.json'
    old.write_text('previous')
    replace = namespace['os'].replace
    def fail_first(source, target):
        if target == old:
            raise OSError('replacement failed')
        return replace(source, target)
    monkeypatch.setattr(namespace['os'], 'replace', fail_first)
    rows = namespace['classify_task_files']([(tmp_path, [first, second])], progress=False)
    assert [r['status'] for r in rows] == ['failed', 'written']
    assert 'replacement failed' in rows[0]['error']
    assert old.read_text() == 'previous'
    assert not list(tmp_path.glob('*.tmp'))


def test_interrupted_batch_retains_completed_reports(tmp_path, monkeypatch):
    namespace = helpers()
    first, second = semantics(tmp_path, 'a'), semantics(tmp_path, 'b')
    output = tmp_path / 'b_tasks.json'
    output.write_text('previous')
    write = namespace['write_task_output']
    def interrupt(path, tree):
        if path == output:
            raise KeyboardInterrupt()
        return write(path, tree)
    monkeypatch.setitem(namespace, 'write_task_output', interrupt)
    rows = []
    with pytest.raises(KeyboardInterrupt):
        namespace['classify_task_files']([(tmp_path, [first, second])], results=rows, progress=False)
    assert len(rows) == 1 and rows[0]['status'] == 'written'
    assert output.read_text() == 'previous'


def test_preview_runs_against_only_temporary_outputs(tmp_path):
    namespace = helpers()
    semantics(tmp_path, 'a', document(section('模板作业', [wrapper(table())])))
    namespace['results'] = namespace['classify_task_files'](
        namespace['discover_semantics'](tmp_path), progress=False)
    namespace.update(PREVIEW_OUTPUTS=1, PREVIEW_TASKS=1)
    exec(compile(code_with('results_df ='), str(APPLICATION_NOTEBOOK), 'exec'), namespace)
    assert len(namespace['preview']) == 1
    assert namespace['preview'].iloc[0]['reasons'] == 'section_title'
