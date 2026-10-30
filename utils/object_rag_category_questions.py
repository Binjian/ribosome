"""Category-only question variant; source locators remain reviewer metadata."""
import re

CATEGORY_SYSTEM_PROMPT = '本问题所属类别：{category}。'


def category_question(question):
    """Remove source addresses, retaining the task and technical identifiers."""
    # Sampled questions always separate the manual/revision prefix with this comma.
    if question.startswith(('根据《', '根据手册')):
        question = question.split('），', 1)[1]
    elif question.startswith('请依据《'):
        if '回答：' in question:
            question = question.split('回答：', 1)[1]
        else:
            # Legacy instruction/alarm questions put their task after a heading quote.
            question = re.sub(r'^请依据《.*?[）)](?:第[\d.]+[章节])?(?:的)?“[^”]*”(?:表)?', '', question)
    question = re.sub(r'要在“[^”]*”中配置“([^”]*)”', r'要配置“\1”', question)
    # These are source-heading pointers, not part of the requested technical task.
    question = re.sub(r'^“[^”]+”列出的报警', '报警', question)
    question = re.sub(r'^“([^”]+)”中“([^”]+)”', r'关于\1，“\2”', question)
    # Keep meaningful task names as ordinary topics, rather than source addresses.
    question = re.sub(r'^在“([^”]+)”中，应如何理解或操作“([^”]+)”',
                      r'关于\1，应如何理解或操作“\2”', question)
    question = re.sub(r'^配置“([^”]+)”时，“([^”]+)”', r'配置\1时，“\2”', question)
    question = re.sub(r'^操作“([^”]+)”时，涉及“([^”]+)”', r'进行\1时，涉及“\2”', question)
    question = re.sub(r'\b\d+(?:\.\d+)+\s+(?=[\u4e00-\u9fff])', '', question)
    question = re.sub(r'本题关注原文记录“.*?”。?', '', question)
    question = question.replace('（该表归类为报警信息）', '')
    question = question.replace('本版本', '所引用证据版本')
    question = question.replace('本条记录', '检索证据').replace('本条目', '该报警')
    question = question.replace('原文给出了', '有记录的').replace('原文记录', '检索证据')
    question = question.replace('表中条目名称', '指令名称').replace('若表中有子码', '若有子码')
    return question.strip()


class CategoryPromptGenerator:
    """Add only the selected category to production system instructions.

    The production request already supplies category and retrieved evidence. Do
    not copy case references, expected facts, source identities or revision labels.
    """
    def __init__(self, generator):
        self.generator = generator

    async def chat(self, **kwargs):
        messages = [dict(message) for message in kwargs['messages']]
        request = next(m['content'] for m in messages if m['role'] == 'user')
        match = re.match(r'Selected category: ([a-z_]+)\n', request)
        if match is None:
            raise ValueError('Missing selected category in production request')
        category_prompt = CATEGORY_SYSTEM_PROMPT.format(category=match.group(1))
        system = next(m for m in messages if m['role'] == 'system')
        system['content'] += '\n' + category_prompt
        return await self.generator.chat(**dict(kwargs, messages=messages))


# Presentation only: preserve raw generation/checkpoint identities and gold isolation.
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from object_rag_evaluation import EvaluationRun, answer_blocks, compatible_review


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'img' and attrs.get('src'):
            self.images.append((attrs.get('alt', 'Source image'), attrs['src']))


def _images(text):
    parser = _Images()
    parser.feed(text)
    yield from parser.images
    for match in re.finditer(r'!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\s]+?))(?:\s+["\'][^\n]*?["\'])?\s*\)', text):
        yield match[1] or 'Source image', match[2] or match[3]


def _local_path(value, source, root):
    """Resolve document-relative assets, rejecting paths outside the project."""
    value = unescape(str(value)).replace('\\', '/')
    url = urlsplit(value)
    if url.scheme or url.netloc or not url.path:
        return None
    path = Path(unquote(url.path))
    if str(path).startswith('/assets/'):
        path = root / str(path).lstrip('/')
    elif not path.is_absolute():
        path = root / path if path.parts[0] == 'assets' else source.parent / path
    path = path.resolve()
    return path if path.is_relative_to(root) else None


def _source_path(record, root):
    source = Path(record.get('source_file', ''))
    return (source if source.is_absolute() else root / source).resolve()


def _section_images(source, line_start):
    """Find images only in the hit's actual leaf subsection, for 'see figure'."""
    if not source.is_file() or not isinstance(line_start, int):
        return ''
    lines = source.read_text(encoding='utf-8-sig').splitlines()
    position = max(0, min(line_start - 1, len(lines) - 1))
    start, depth = position, None
    for i in range(position, -1, -1):
        match = re.match(r'^(#{1,6})\s+', lines[i])
        if match:
            start, depth = i, len(match[1])
            break
    if depth is None:
        return ''
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = re.match(r'^(#{1,6})\s+', lines[i])
        if match and len(match[1]) <= depth:
            end = i
            break
    return '\n'.join(lines[start:end])


def evidence_locations(state, project_root):
    """Exact locations of all returned embeddings, separate from generated claims."""
    root = Path(project_root).resolve()
    result = state.get('answer') or state.get('partial_answer') or {}
    retrieval = state.get('retrieval') or result
    hits, context = retrieval.get('hits', []), retrieval.get('context', '')
    if not hits:
        return '#### Retrieved embedding locations\n\nNo retrieved source locations are available.'
    blocks = ['#### Retrieved embedding locations',
              'Locations below come from retrieved records. Image links are relative to the project root.']
    seen_images = set()
    for number, hit in enumerate(hits, 1):
        record, manual = hit['record'], hit.get('manual', {})
        attributes = record.get('attributes', {})
        identifier = record['id']
        # Labels are assigned after budget packing, so hit order is not E-number order.
        matching = [m for m in re.finditer(r'^\[(E\d+)\] ([^\n]*)\n(.*?)(?=^\[E\d+\] |\Z)', context, re.M | re.S)
                    if m[2].rstrip().endswith('| ' + identifier)]
        label = matching[0][1] if matching else f'Hit {number}'
        status = 'supplied to generation' if matching else 'not supplied to generation'
        headings = ' → '.join(record.get('section_headings', [])) or 'Subsection unavailable in indexed metadata'
        source = _source_path(record, root)
        start, end = attributes.get('line_start'), attributes.get('line_end')
        if source.is_relative_to(root):
            relative = source.relative_to(root).as_posix()
            target = quote(relative, safe='/-._~')
            if start is not None:
                target += f'#L{start}' + (f'-L{end}' if end is not None else '')
            location = f'[{source.name}](<{target}>)'
            # DOCX conversion keeps a nested Markdown directory for each manual.
            parts = source.relative_to(root).parts
            if '.md' in parts:
                index = parts.index('.md')
                original = root.joinpath(*parts[:index], *parts[index + 1:-2], source.stem + '.docx')
                if original.is_file():
                    original_target = quote(original.relative_to(root).as_posix(), safe='/-._~')
                    location += f'; original document [{original.name}](<{original_target}>)'
        else:
            location = 'Source is outside the project; no project-relative link is available'
        blocks.append(f'**{label} — {headings}**\n\n{location}; lines {start if start is not None else "unknown"}–{end if end is not None else "unknown"}; '
                      f'corpus `{manual.get("profile", "unspecified")}`, revision `{manual.get("revision", "unspecified")}`; '
                      f'object `{identifier}`; **{status}**.')
        text = record.get('content', '') + '\n' + '\n'.join(m[3] for m in matching)
        images = list(_images(text))
        for image in attributes.get('images', []):
            if isinstance(image, str):
                images.append(('Source image', image))
            elif isinstance(image, dict):
                path = image.get('src') or image.get('path') or image.get('url')
                if path:
                    images.append((image.get('alt', 'Source image'), path))
        if not images and re.search(r'如[下上]?图|见图|图\s*\d|figure\s*\d', text, re.I) and source.is_relative_to(root):
            images = list(_images(_section_images(source, start)))
            if images:
                blocks.append('Images in the referenced source subsection:')
        for alt, value in images:
            path = _local_path(value, source, root)
            if path is None:
                continue
            relative = path.relative_to(root).as_posix()
            if relative in seen_images:
                continue
            seen_images.add(relative)
            if path.is_file():
                alt = re.sub(r'[\[\]\n]', ' ', alt)
                blocks.append(f'![{alt}](<{quote(relative, safe="/-._~")}>)')
            else:
                blocks.append(f'Image file unavailable: `{relative}`.')
    return '\n\n'.join(blocks)


def located_answer_blocks(case, state, project_root):
    blocks = answer_blocks(case, state)
    blocks[2] += '\n\n' + evidence_locations(state, project_root)
    return blocks


class CategoryEvaluationRun(EvaluationRun):
    """Render provenance for existing and new results without changing retrieval."""
    project_root = Path(__file__).resolve().parents[1]

    def for_top_k(self, top_k):
        if top_k == self.top_k:
            return self
        return type(self)(self.app, self.cases, self.root_directory,
                          top_k=top_k, settings=self.settings)

    def show_answers(self, cases):
        from IPython.display import Markdown, display
        for case in cases:
            state = self.load(case)
            if state:
                for block in located_answer_blocks(case, state, self.project_root):
                    display(Markdown(block))

    def inspect(self, case_id):
        from IPython.display import Markdown, display
        case = next(c for c in self.cases if c['id'] == case_id)
        state = self.load(case)
        for block in located_answer_blocks(case, state, self.project_root):
            display(Markdown(block))
        print(state.get('retrieval', {}).get('context', 'Retrieval not completed'))
        # Original references stay reviewer-only, as in the base inspection helper.
        display(case['references'])
        display(compatible_review(case, state))
