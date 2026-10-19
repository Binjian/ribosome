"""Offline image-ignore matching, provider boundaries and checkpoint behavior."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import threading

from PIL import Image, ImageDraw
import pytest

from ribosome.core.dom.summary import object as sut
from ribosome.core.dom.summary import object_utils
from utils import summary_object_asyncio as batch


@pytest.fixture
def image_root(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    (tmp_path / 'pyproject.toml').write_text('')
    object_utils._cached_image_file_info.cache_clear()
    return tmp_path


def artwork():
    image = Image.new('RGB', (160, 100))
    image.putdata([(30 + x, 25 + y, 50 + (x + y) // 3)
                   for y in range(100) for x in range(160)])
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 52, 65), fill=(230, 35, 25))
    draw.ellipse((82, 15, 142, 85), fill=(30, 150, 220))
    return image


def save_reference(root, image=None, name='nested/reference.png'):
    path = root / 'assets' / 'images_ignored' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    (artwork() if image is None else image).save(path)
    return path


def document(root, source):
    file = root / 'manual.md'
    file.write_text(source, encoding='utf-8')
    return file


def walk_image(root, node, legacy=False):
    function = sut.summary_node_async if legacy else sut.summarize_node_async
    return function(node=node, root_path=root, file_path=root / 'manual.md',
                    client=object(), model='offline', table_count=0 if legacy else {'count': 0},
                    section_count=0 if legacy else {'count': 0}, leaf_min_len=99999)


def image_node(source='source.png'):
    return {'t': 'Image', 'c': [['', [], []], [{'t': 'Str', 'c': 'IGNORED ALT'}], [source, '']]}


@pytest.mark.parametrize('variant', ['identical', 'resized', 'recompressed', 'minor_pixels'])
def test_near_duplicates_match(image_root, variant):
    reference = save_reference(image_root)
    source = image_root / ('source.jpg' if variant == 'recompressed' else 'source.png')
    image = artwork()
    if variant == 'resized':
        image = image.resize((320, 200), Image.Resampling.LANCZOS)
    elif variant == 'minor_pixels':
        image.putpixel((75, 50), (120, 120, 120))
    image.save(source, **({'quality': 90} if variant == 'recompressed' else {}))
    snapshot = object_utils._load_ignored_image_snapshot()
    assert object_utils._match_ignored_image(source, snapshot) == str(reference)


@pytest.mark.parametrize('white', [False, True])
def test_transparent_artwork_matches_after_resizing_but_not_a_blank_image(image_root, white):
    image = Image.new('RGBA', (160, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = 'white' if white else '#bc2010'
    draw.rectangle((17, 21, 60, 68), fill=color)
    draw.ellipse((90, 30, 140, 80), fill=color)
    reference = save_reference(image_root, image)
    source = image_root / 'source.png'
    image.resize((320, 200), Image.Resampling.LANCZOS).save(source)
    snapshot = object_utils._load_ignored_image_snapshot()
    assert object_utils._match_ignored_image(source, snapshot) == str(reference)
    blank = image_root / 'blank.png'
    Image.new('RGB', (160, 100), 'white').save(blank)
    assert object_utils._match_ignored_image(blank, snapshot) is None


def test_different_artwork_and_aspect_ratios_do_not_match(image_root):
    save_reference(image_root)
    source = image_root / 'source.png'
    artwork().transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(source)
    snapshot = object_utils._load_ignored_image_snapshot()
    assert object_utils._match_ignored_image(source, snapshot) is None
    artwork().resize((200, 100)).save(source)
    assert object_utils._match_ignored_image(source, snapshot) is None


def test_exif_orientation_is_normalized(image_root):
    image = artwork()
    reference = save_reference(image_root, image)
    source = image_root / 'rotated.png'
    exif = Image.Exif()
    exif[274] = 6  # Rotate the stored pixels clockwise when displayed.
    image.transpose(Image.Transpose.ROTATE_90).save(source, exif=exif)
    assert object_utils._match_ignored_image(source, object_utils._load_ignored_image_snapshot()) == str(reference)


@pytest.mark.parametrize('aspect,hash_bits,pixel_difference,expected', [
    (0.98, 4, 0.03, True), (0.979, 4, 0.03, False),
    (1, 5, 0, False), (1, 4, 0.031, False),
])
def test_matching_limits_include_the_boundary(aspect, hash_bits, pixel_difference, expected):
    # These RGB samples represent the exact average channel differences requested.
    count = 1000
    difference = int(round(pixel_difference * count * 255))
    values = bytes([255] * (difference // 255) + [difference % 255]
                   + [0] * (count - difference // 255 - 1))
    reference = (1, ((0, bytes(count)), (0, bytes(count))))
    candidate = (aspect, (((1 << hash_bits) - 1, values), ((1 << hash_bits) - 1, values)))
    assert object_utils._image_fingerprints_match(reference, candidate) is expected
    # Both backgrounds must match, not just the first one.
    if expected:
        assert not object_utils._image_fingerprints_match(reference, (1, (reference[1][0], (255, bytes(count)))))


@pytest.mark.parametrize('directory', ['missing', 'empty', 'non_images'])
def test_missing_or_empty_set_disables_filtering(image_root, directory):
    folder = image_root / 'assets' / 'images_ignored'
    if directory != 'missing': folder.mkdir(parents=True)
    if directory == 'non_images': (folder / 'README.txt').write_text('not an image')
    snapshot = object_utils._load_ignored_image_snapshot()
    assert snapshot.references == ()
    assert len(snapshot.signature) == 64


def test_project_root_discovery_is_used_without_environment(image_root, monkeypatch):
    reference = save_reference(image_root)
    monkeypatch.delenv('PROJ_ROOT')
    monkeypatch.chdir(image_root)
    assert object_utils._load_ignored_image_snapshot().references[0][0] == str(reference)


def test_unreadable_reference_warns_once_and_other_references_still_work(image_root, monkeypatch):
    valid = save_reference(image_root)
    folder = valid.parent
    corrupt = folder / 'corrupt.png'
    corrupt.write_bytes(b'not a PNG')
    unreadable = folder / 'unreadable.png'
    artwork().save(unreadable)
    original = Path.read_bytes
    def read(path):
        if path == unreadable: raise PermissionError('fixture access denied')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.warns(RuntimeWarning, match='ignored-image reference') as messages:
        first = object_utils._load_ignored_image_snapshot()
        second = object_utils._load_ignored_image_snapshot()
    assert len(messages) == 2
    assert [path for path, _ in first.references] == [str(valid)]
    assert first == second


@pytest.mark.parametrize('source', ['https://example.com/picture.png', '//example.com/picture.png', 'source.svg', 'source.png'])
def test_remote_unsupported_or_unreadable_sources_do_not_match(image_root, source, monkeypatch):
    save_reference(image_root)
    snapshot = object_utils._load_ignored_image_snapshot()
    if source == 'source.png':
        (image_root / source).write_bytes(b'corrupt source')
        source = str(image_root / source)
    else:
        def fail(*args): raise AssertionError('unsupported sources should not be read')
        monkeypatch.setattr(object_utils, '_image_file_info', fail)
    assert object_utils._match_ignored_image(source, snapshot) is None


def test_animated_images_follow_the_provider_path(image_root, monkeypatch):
    save_reference(image_root)
    source = image_root / 'animated.gif'
    artwork().save(source, save_all=True, append_images=[artwork().transpose(Image.Transpose.FLIP_LEFT_RIGHT)], duration=100, loop=0)
    assert object_utils._match_ignored_image(source, object_utils._load_ignored_image_snapshot()) is None
    calls = []
    async def provider(**kwargs):
        calls.append(kwargs['image_link'])
        return 'animation'
    monkeypatch.setattr(sut, 'get_image_summary_async', provider)
    result = asyncio.run(walk_image(image_root, image_node('animated.gif')))
    assert result['s'] == 'animation' and calls == [str(source)]


@pytest.mark.parametrize('legacy', [False, True])
def test_ignored_image_nodes_skip_all_providers_and_clear_old_summaries(image_root, monkeypatch, legacy):
    reference = save_reference(image_root)
    artwork().save(image_root / 'source.png')
    node = image_node()
    original = deepcopy(node['c'])
    node['s'] = 'stale summary'
    async def fail(*args, **kwargs): raise AssertionError('ignored image must not call a provider')
    for name in ['get_image_summary_async', 'get_image_summary_response_async', 'get_summary_response_async', 'get_text_summary_response_async']:
        monkeypatch.setattr(sut, name, fail)
    result = asyncio.run(walk_image(image_root, node, legacy))
    assert result['s'] == '' and result['summary_ignored'] is True
    assert result['ignored_reference'] == str(reference) and result['c'] == original
    assert object_utils._ignored_images.get() is None


@pytest.mark.parametrize('tree', [False, True])
def test_fully_ignored_figures_preserve_structure_and_complete_progress(image_root, monkeypatch, tree):
    reference = save_reference(image_root)
    artwork().save(image_root / 'source.png')
    file = document(image_root, '![IGNORED ALT](source.png)\n图 1 Ignored caption\n')
    source = file.read_bytes()
    original = sut.extract_markdown_objects(file)[0]
    original_c = next(sut._object_nodes(sut.extract_markdown_object_tree(file)))['c'] if tree else None
    bars = []
    class Bar:
        def __init__(self, **kwargs):
            self.total, self.completed, self.closed = kwargs['total'], 0, False
            bars.append(self)
        def update(self, amount): self.completed += amount
        def close(self): self.closed = True
    async def fail(*args, **kwargs): raise AssertionError('fully ignored figure must make no requests')
    monkeypatch.setattr(sut, 'get_image_summary_async', fail)
    monkeypatch.setattr(sut, 'get_summary_response_async', fail)
    monkeypatch.setattr('tqdm.auto.tqdm', Bar)
    function = sut.summarize_markdown_object_tree_async if tree else sut.summarize_markdown_objects_async
    options = {'leaf_min_len': 99999} if tree else {}
    result = asyncio.run(function(file, object(), 'offline', show_progress=True, **options))
    if tree:
        wrapper = next(sut._object_nodes(result))
        figure = wrapper['object']
        assert wrapper['s'] == ''
        assert wrapper['c'] == original_c
        assert 'IGNORED ALT' not in result['summary'] and 'Ignored caption' not in result['summary']
    else:
        figure = result[0]
    assert figure['summary'] == '' and figure['summary_ignored'] is True
    assert figure['images'][0]['ignored_reference'] == str(reference)
    assert {key: figure[key] for key in original if key != 'images'} == {
        key: value for key, value in original.items() if key != 'images'}
    assert {key: figure['images'][0][key] for key in original['images'][0]} == original['images'][0]
    assert figure['content'] == original['content'] and file.read_bytes() == source
    assert bars[0].total == bars[0].completed == 1 and bars[0].closed


def test_mixed_figure_prompts_exclude_ignored_alt_and_source(image_root, monkeypatch):
    reference = save_reference(image_root)
    artwork().save(image_root / 'source.png')
    artwork().transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(image_root / 'keep.png')
    file = document(image_root, '![IGNORED ALT](source.png)\n![KEEP ALT](keep.png)\n图 1 Shared caption\n')
    calls, prompts = [], []
    async def image(**kwargs):
        calls.append(kwargs['image_link'])
        return 'retained visual'
    async def text(client, content, **kwargs):
        prompts.append(content)
        return 'figure summary'
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    figure, = asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert calls == [str(image_root / 'keep.png')] and len(prompts) == 1
    payload = json.loads(prompts[0].split('\n', 1)[1])
    assert payload == {'caption': '图 1 Shared caption', 'image_descriptions': [{'alt': 'KEEP ALT', 'description': 'retained visual'}]}
    assert 'IGNORED ALT' not in prompts[0] and 'source.png' not in prompts[0]
    assert 'IGNORED ALT' in figure['content']
    assert figure['images'][0]['ignored_reference'] == str(reference)
    assert 'summary_ignored' not in figure and 'ignored_reference' not in figure['images'][1]


def test_reruns_refresh_skip_markers_after_reference_removal(image_root, monkeypatch):
    reference = save_reference(image_root)
    artwork().save(image_root / 'source.png')
    file = document(image_root, '![alt](source.png)\n')
    figure = sut.extract_markdown_objects(file)[0]
    node = image_node()
    calls = []
    async def provider(**kwargs):
        calls.append('image')
        return 'visual'
    async def text(**kwargs):
        calls.append('text')
        return 'summary'
    monkeypatch.setattr(sut, 'get_image_summary_async', provider)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    async def run():
        await sut._summarize_markdown_object_async(figure, object(), 'offline', 'en', require_summary=True)
        await walk_image(image_root, node)
    asyncio.run(run())
    assert not calls and figure['summary_ignored'] and node['summary_ignored']
    reference.unlink()
    asyncio.run(run())
    assert calls == ['image', 'text', 'image']
    assert figure['summary'] == 'summary' and node['s'] == 'visual'
    assert 'summary_ignored' not in figure and 'summary_ignored' not in node
    assert 'ignored_reference' not in node and 'ignored_reference' not in figure['images'][0]


def test_source_decode_failures_keep_provider_errors(image_root, monkeypatch):
    save_reference(image_root)
    (image_root / 'source.png').write_bytes(b'broken')
    async def provider(**kwargs): raise RuntimeError('existing provider failure')
    monkeypatch.setattr(sut, 'get_image_summary_async', provider)
    with pytest.raises(ValueError, match='existing provider failure'):
        asyncio.run(walk_image(image_root, image_node()))


def test_fingerprints_are_cached_and_stat_changes_refresh_them(image_root):
    reference = save_reference(image_root)
    artwork().save(image_root / 'source.png')
    first = object_utils._load_ignored_image_snapshot()
    misses = object_utils._cached_image_file_info.cache_info().misses
    assert object_utils._load_ignored_image_snapshot() == first
    assert object_utils._cached_image_file_info.cache_info().misses == misses
    old_stat = reference.stat()
    artwork().transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(reference)
    os.utime(reference, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))
    second = object_utils._load_ignored_image_snapshot()
    assert second.signature != first.signature
    assert object_utils._match_ignored_image(image_root / 'source.png', first) == str(reference)
    assert object_utils._match_ignored_image(image_root / 'source.png', second) is None


def test_reference_digest_detects_edits_with_preserved_size_and_timestamp(image_root):
    reference = save_reference(image_root, Image.new('RGB', (160, 100), 'red'), 'reference.bmp')
    first = object_utils._load_ignored_image_snapshot()
    stat = reference.stat()
    Image.new('RGB', (160, 100), 'blue').save(reference)
    assert reference.stat().st_size == stat.st_size
    os.utime(reference, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = object_utils._load_ignored_image_snapshot()
    assert second.signature != first.signature
    assert second.references[0][1] != first.references[0][1]


def test_run_uses_one_immutable_snapshot_and_decodes_off_thread(image_root, monkeypatch):
    reference = save_reference(image_root)
    artwork().transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(image_root / 'keep.png')
    artwork().save(image_root / 'source.png')
    file = document(image_root, '![keep](keep.png)\n\nIntervening prose.\n\n![ignored](source.png)\n')
    thread_ids, snapshots, calls = [], [], []
    original = object_utils._image_fingerprint
    def fingerprint(image):
        thread_ids.append(threading.get_ident())
        return original(image)
    async def image(**kwargs):
        snapshots.append(object_utils._ignored_images.get())
        calls.append(kwargs['image_link'])
        reference.unlink()  # The second image must still use this run's snapshot.
        return 'visual'
    async def text(**kwargs): return 'summary'
    monkeypatch.setattr(object_utils, '_image_fingerprint', fingerprint)
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', 1)
    result = asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert calls == [str(image_root / 'keep.png')]
    assert result[1]['summary_ignored'] and result[1]['summary'] == ''
    assert thread_ids and all(identity != threading.get_ident() for identity in thread_ids)
    assert isinstance(snapshots[0].references, tuple) and object_utils._ignored_images.get() is None


class Progress:
    def __init__(self, total, **kwargs): self.total, self.n = total, 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def refresh(self): pass
    def set_postfix(self, **kwargs): pass
    def set_postfix_str(self, text, **kwargs): pass
    @classmethod
    def write(cls, message): pass


def test_reference_and_setting_changes_invalidate_batch_checkpoints(image_root, monkeypatch):
    file = document(image_root, '# Manual\n\n- text\n')
    checkpoint = image_root / 'checkpoint.json'
    async def text(**kwargs): return 'summary'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    def run():
        return asyncio.run(batch.summarize_object_documents(
            [file], object(), 'offline', checkpoint_path=checkpoint,
            leaf_min_len=99999, progress_factory=Progress))[0]
    assert not run()['resumed'] and run()['resumed']
    reference = save_reference(image_root)
    assert not run()['resumed'] and run()['resumed']
    old_stat = reference.stat()
    artwork().transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(reference)
    os.utime(reference, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))
    assert not run()['resumed'] and run()['resumed']
    reference.unlink()
    assert not run()['resumed'] and run()['resumed']
    monkeypatch.setattr(object_utils, '_IGNORED_IMAGE_HASH_DISTANCE', 3)
    assert not run()['resumed'] and run()['resumed']
    assert object_utils._ignored_images.get() is None and not batch._batch_active


def test_batch_snapshot_failure_restores_active_state(image_root, monkeypatch):
    file = document(image_root, '- text\n')
    async def fail(): raise RuntimeError('snapshot setup failed')
    monkeypatch.setattr(sut, '_prepare_ignored_images', fail)
    with pytest.raises(RuntimeError, match='snapshot setup failed'):
        asyncio.run(batch.summarize_object_documents([file], object(), 'offline', progress_factory=Progress))
    assert not batch._batch_active and object_utils._ignored_images.get() is None


def test_cancelling_decode_waits_for_threads_and_restores_context(image_root, monkeypatch):
    save_reference(image_root)
    artwork().save(image_root / 'source.png')
    file = document(image_root, '![alt](source.png)\n')
    original = object_utils._image_file_info
    released = threading.Event()
    finished = threading.Event()
    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        def blocked(path, **kwargs):
            if path == image_root / 'source.png':
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert released.wait(3)
                finally:
                    finished.set()
            return original(path, **kwargs)
        monkeypatch.setattr(object_utils, '_image_file_info', blocked)
        task = asyncio.create_task(sut.summarize_markdown_objects_async(file, object(), 'offline'))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            released.set()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
            assert finished.is_set() and object_utils._ignored_images.get() is None
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        finally:
            released.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
