"""Build reproducible, source-extractive category-only evaluation questions.

The source corpus is read-only. Each case records exact Markdown lines and the
source hash. Distinct instruction facets are separate evaluation targets; no
model answers, summaries, or synthetic facts are used as gold evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
import sys
import unicodedata

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.rag.rag_evaluation_samples import plain, load_sample_bank

VERSION = 1
CATEGORY_BY_MANUAL = {
    'SX322001': ('general_operation', '机器人通用操作'),
    'SX322002': ('palletizing', '码垛'),
    'SX322003': ('arc_welding', '弧焊'),
    'SX322004': ('laser_welding', '激光焊'),
    'SX322005': ('vision', '视觉'),
    'SX322006': ('grinding', '打磨'),
    'SX322007': ('spot_welding', '点焊'),
    'SX322008': ('teaching_free_welding', '免示教焊接'),
    'SX322009': ('offline_interface', '离线接口'),
    'SX322010': ('load_analysis', '负载分析'),
    'SX322011': ('material_handling', '搬运'),
    'SX322012': ('adhesive_dispensing', '涂胶'),
    'SX322013': ('stud_welding', '螺柱焊'),
    'SX322014': ('spr_riveting', 'SPR铆接'),
    'SX322015': ('tool_quick_change', '快换'),
    'SX322016': ('fds', 'FDS'),
    'SX322017': ('tracking', '跟随'),
    'SX322018': ('pneumatic_spot_welding', '气动点焊'),
}
SOURCE_HINT = re.compile(
    r'根据手册|根据《|请依据|手册|SX\d{5,}|第[\d.一二三四五六七八九十]+[章节]|'
    r'\bV\d+\.\d+|[（(][ABC]-\d|(?:assets|code-docs)[/\\]|\.md\b|\.json\b|对象\s*ID|行号', re.I)
GENERIC = {'功能', '格式', '说明', '举例', '示例', '参数', '参数说明', '名称', '序号', '备注',
           '类型', '注意', '注', '注意事项', '报警码', '故障代码', '报警信息', '变量号', '变量类型',
           '值', '取值', '取值范围', '返回值', '目录', '概要', '概述', '引言', '文件历次修订记录',
           '内容', '特别注意', '使用说明', '功能说明', '作业示例', '参数定义'}


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return sha256(value.encode('utf-8')).hexdigest()


def title(text):
    text = plain(text).replace('**', '').replace('\\_', '_').strip(' #*：:，,')
    text = text.replace('新松机器人负载分析软件说明', '负载分析软件').replace('新松机器人', '')
    return re.sub(r'^\s*\d+(?:\.\d+)*[.、)）\s]*', '', text).strip()


def good_label(text, maximum=65):
    return (2 <= len(text) <= maximum and text not in GENERIC and
            not SOURCE_HINT.search(text) and not re.search(r'本文|本文件|本资料|版权|主要有|如下|所示|例如|股份有限公司|著作权', text) and not re.search(r'[?？。；;，,]|\.\.\.|…|如图|见图|图\s*\d|如下|上述|\]\(|!\[', text) and
            not re.fullmatch(r'[\d\s.\-—–~%:：/()（）]+', text) and
            not re.fullmatch(r'(?:举例|示例|说明|内容|注|注意)\s*\d*', text))


def objects(node):
    """Visit authored object wrappers, avoiding their duplicate children copy."""
    if isinstance(node, dict):
        if isinstance(node.get('object'), dict):
            yield node['object']
        for key, value in node.items():
            if key != 'object':
                yield from objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from objects(value)


def cells(row):
    return [plain(value) for value in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.S | re.I)]


def html_rows(text):
    return [cells(row) for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', text, re.S | re.I)]


class Source:
    def __init__(self, root, path, profile):
        self.path = path
        self.relative = path.relative_to(root).as_posix()
        self.profile = profile
        self.raw = path.read_text(encoding='utf-8')
        self.lines = self.raw.splitlines()
        self.sha = sha256(path.read_bytes()).hexdigest()
        self.category, self.name = CATEGORY_BY_MANUAL[re.search(r'SX\d+', path.name)[0]]
        revision = re.search(r'[（(]([ABC]-\d+)[）)]|[-_]([ABC]-\d+|V\d+\.\d+)$', path.stem)
        self.revision = next((v for v in revision.groups() if v), '') if revision else ''
        self.paths = []
        stack = []
        self.sections = []
        for number, line in enumerate(self.lines, 1):
            match = re.match(r'^(#{1,6})\s+(.+)', line)
            if match:
                level, heading = len(match[1]), plain(match[2]).replace('**', '')
                stack = [(depth, h) for depth, h in stack if depth < level]
                stack.append((level, heading))
                self.sections.append((number, level, heading))
            self.paths.append([h for _, h in stack])

    def context(self, start):
        headings = self.paths[start - 1]
        clean = [title(h) for h in headings]
        useful = [h for h in clean if good_label(h) and h not in {'指令说明', '参数设置', '基础配置',
                   '指令', '操作说明', '操作步骤', '常见报警及解决方法', '报警说明'}]
        topic = useful[-1] if useful else self.name
        if len(useful) > 1 and re.fullmatch(r'C[+#]+(?:版本)?', topic):
            topic = useful[-2] + '的' + topic
        if topic == self.name or self.name in topic:
            return topic
        return self.name + '的' + topic

    def case(self, start, end, question, kind, facet, representation, facts=None, source_id=None):
        if not (1 <= start <= end <= len(self.lines)):
            return None
        if len(question) > 220 or SOURCE_HINT.search(question) or question.count('？') != 1:
            return None
        excerpt = '\n'.join(self.lines[start - 1:end])
        text = plain(excerpt)
        if not 12 <= len(text) <= 18000 or not re.search(r'[\u4e00-\u9fff]', text):
            return None
        facts = [' '.join(fact.split()) for fact in (facts or [text]) if fact.strip()]
        if not facts or any(fact not in text for fact in facts):
            return None
        headings = self.paths[start - 1]
        if not headings or not any(re.match(r'^\d+', h) for h in headings) or any('修订' in h or '目录' == title(h) for h in headings):
            return None
        section = re.match(r'^\d+(?:\.\d+)*', headings[-1])
        case = dict(id='object_sample_' + digest([self.relative, start, end, facet, question])[:24],
                    category=self.category, kind=kind, profile=self.profile, revision=self.revision,
                    section=section[0] if section else '', section_title=headings[-1],
                    heading_path=headings, source_md=self.relative, question=question,
                    anchors=[], expected_facts=facts, representation=representation, question_focus=facet,
                    references=[dict(markdown=self.relative, line_start=start, line_end=end,
                                     excerpt=excerpt, sha256=self.sha)])
        if source_id is not None:
            case['source_object_id'] = source_id
        # Exact technical names help retrieval checks without leaking references.
        for token in re.findall(r'(?<![\w])[A-Za-z][A-Za-z0-9_]*(?:\[[^\]]+\])?', question):
            if len(token) >= 2 and token in text and token not in case['anchors']:
                case['anchors'].append(token)
        return case


def instruction_candidates(source, obj):
    rows = html_rows(obj['content'])
    if not rows or sum('功能' in row for row in rows) != 1:
        return
    first = rows[0]
    if len(first) < 3 or first[1] != '功能' or not good_label(first[0]):
        return
    label = first[0]
    subject = f'{source.name}中的“{label}”'
    fields = {'功能': ' '.join(first[2:])}
    for row in rows[1:]:
        if row and row[0] in {'格式', '直接格式', '间接格式', '说明', '举例'}:
            fields[row[0]] = ' '.join(row[1:])
        elif row and '说明' in fields and re.match(r'(?:<)?参数\d', row[0]):
            fields['说明'] += ' ' + ' '.join(row)
    args = (obj['line_start'], obj['line_end'])
    base = dict(representation=obj.get('type', 'table_row_group'), source_id=obj.get('id'))
    if len(fields['功能']) >= 12:
        yield source.case(*args, f'{subject}实现什么功能？', 'instruction', 'function',
                          facts=[fields['功能']], **base)
    formats = [fields[key] for key in ('格式', '直接格式', '间接格式') if fields.get(key)]
    if formats:
        yield source.case(*args, f'{subject}的调用格式应如何书写？', 'instruction', 'syntax',
                          facts=formats, **base)
    parameters = fields.get('说明', '')
    if len(parameters) >= 15 and re.search(r'参数|范围|单位|表示|变量|含义', parameters):
        # Individual parameter targets prevent a large instruction table from
        # collapsing into one broad, unscorable question.
        matches = list(re.finditer(r'(?:<|＜)?参数\s*(\d+)(?:>|＞)?\s*[：:]?', parameters))
        if matches:
            for i, match in enumerate(matches):
                fact = parameters[match.start(): matches[i + 1].start() if i + 1 < len(matches) else len(parameters)].strip()
                if len(fact) < 12:
                    continue
                yield source.case(*args, f'{subject}的参数{match[1]}表示什么？', 'parameter',
                                  'parameter_' + match[1], facts=[fact], **base)
        else:
            yield source.case(*args, f'{subject}的参数有哪些含义和使用要求？', 'parameter',
                              'parameters', facts=[parameters], **base)
    example = fields.get('举例', '')
    if len(example) >= 30 and re.search(r'//|；|;', example):
        # Ask for an evidence-supported example, never invent numeric operands.
        yield source.case(*args, f'如何用{subject}组织一个调用示例？', 'instruction', 'example',
                          facts=[example], **base)


def row_candidates(source, obj):
    rows = html_rows(obj['content'])
    if len(rows) != 1:
        return
    row = rows[0]
    if len(row) < 2 or any(len(v) > 2200 for v in row):
        return
    start, end = obj['line_start'], obj['line_end']
    topic = source.context(start)
    base = dict(representation='table_row', source_id=obj.get('id'))
    code = re.fullmatch(r'(?:报警[：:]?\s*)?(\d{4,6})', row[0])
    if code and len(row) >= 2 and good_label(row[1], 80):
        # Headers must identify this as an alarm table, not a numbered data row.
        headings = ' '.join(source.paths[start - 1])
        if re.search(r'报警|故障', headings) and not re.search(r'警告|提示信息', headings) and not any(re.search(r'提示类|警告类|^(?:提示|警告)(?:信息)?$', value.strip()) for value in row):
            fact = ' '.join(row)
            if len(fact) >= 20:
                has_remedy = bool(re.search(r'检查|确认|联系|调整|更换|重新|复位|重启|恢复|修改|配置|连接|接线|清除|清理|断开|排除|设置', ' '.join(row[2:])))
                question = (f'{source.name}出现报警码{code[1]}时，应如何诊断和处理？' if has_remedy else
                            f'{source.name}的报警码{code[1]}表示什么异常？')
                yield source.case(start, end, question, 'alarm',
                    'alarm_diagnosis' if has_remedy else 'alarm_meaning', facts=[fact], **base)
        return
    label = row[0]
    if not good_label(label, 48) or re.match(r'(?:参数|功能|说明|举例|直接格式|间接格式)', label):
        return
    body = ' '.join(row[1:]).strip()
    if len(body) < 16 or not re.search(r'[\u4e00-\u9fff]', body):
        return
    # Restrict generic tables to concrete identifiers, signal labels, and
    # meaningful configuration names; don't turn headers into questions.
    if re.search(r'修订|版本|日期|负责人|编制|审核|批准|页码|图号', label + body):
        return
    if not re.search(r'表示|设置|范围|单位|对应|控制|用于|检查|参数|变量|输入|输出|信号|状态|有效|无效|允许|禁止|必须|需要|编号|启动|停止|开关|[：:]', body):
        return
    if all(value in GENERIC or len(value) < 4 for value in row[1:] if value):
        return
    if re.search(r'^[A-Z][A-Z_0-9]*(?:\[.*\])?$|信号|输入|输出|字节|变量|端口|地址|速度|延时|时间|压力|电流|电压|编号|使能|允许|选择|模式|参数|范围|检查|状态', label):
        question = f'在{topic}中，“{label}”的含义和使用要求是什么？'
        yield source.case(start, end, question, 'parameter', 'table_mapping', facts=[body], **base)


def prose_candidates(source):
    for start, raw in enumerate(source.lines, 1):
        if not raw.strip() or re.match(r'\s*[<#|!]', raw):
            continue
        text = plain(raw)
        text = text.replace('**', '')
        text = re.sub(r'^\s*(?:\d+[.、)）]\s*|[-*]\s+)', '', text)
        match = re.match(r'([^：:]{2,45})[：:]\s*(.{12,})$', text)
        if not match:
            continue
        label, body = match.groups()
        label = label.strip(' *').replace('\\_', '_').replace('\\*', '*')
        if not good_label(label, 45) or len(body) > 1000 or re.search(r'^[\s\d\W]+$', body):
            continue
        topic = source.context(start)
        subject = f'{topic}中的“{label}”'
        if label in {'路径', '进入路径', '界面路径', '操作路径', '配置路径', '设置路径'}:
            question, kind, focus = f'如何进入{topic}的操作界面？', 'procedure', 'menu_path'
        elif label in {'函数声明', '函数原型'}:
            language = 'C#' if 'public static' in body else 'C++'
            question, kind, focus = f'{topic}的{language}函数声明应如何书写？', 'instruction', 'api_signature'
        elif label in {'功能描述', '功能说明'}:
            question, kind, focus = f'{topic}实现什么功能？', 'instruction', 'api_function'
        elif label in {'返回值说明', '返回值描述'}:
            question, kind, focus = f'{topic}的返回值分别表示什么？', 'parameter', 'return_values'
        elif re.search(r'范围|取值|[0-9]\s*[-~～]\s*[0-9]', body) and re.search(r'范围|取值', body):
            question, kind, focus = f'{subject}有哪些取值要求？', 'parameter', 'value_range'
        elif re.search(r'单位[为是：:]', body):
            question, kind, focus = f'{subject}采用什么单位？', 'parameter', 'unit'
        elif re.search(r'点击|单击|双击|勾选|拖动|选中|下拉|输入框|按钮|菜单|界面|窗口|选择.*后|选择.*项', body):
            question, kind, focus = f'如何操作{subject}？', 'procedure', 'operation'
        elif re.search(r'必须|需先|禁止|不允许|前提|只有.*才|条件下有效', body) or (
                re.search(r'当(?!前)|若.*则|若.*时|仅.*时.*有效', body) and
                re.search(r'条件|检查|检测|允许|屏蔽|触发|巡检|有效|监控|报警|开关', label)):
            question, kind, focus = f'{subject}有哪些生效条件或使用限制？', 'prerequisite', 'conditions'
        else:
            question, kind, focus = f'{subject}有什么作用？', 'parameter', 'meaning'
        yield source.case(start, start, question, kind, focus, 'prose', facts=[plain(raw)])


def section_candidates(source):
    """Add substantial procedural/SDK sections as coherent retrieval targets."""
    for index, (start, depth, heading) in enumerate(source.sections):
        clean = title(heading)
        if not good_label(clean, 70) or re.search(r'报警|故障|附录|修订|主要变化|产品简介', clean):
            continue
        end = len(source.lines)
        # Leaf sections only: parent sections can be hundreds of pages long.
        if index + 1 < len(source.sections):
            next_start, next_depth, _ = source.sections[index + 1]
            if next_depth > depth:
                continue
            end = next_start - 1
        raw = '\n'.join(source.lines[start:end])
        content = plain(raw)
        if not 80 <= len(content) <= 4000 or not re.search(r'[\u4e00-\u9fff]', content):
            continue
        topic = source.context(start)
        facts = [plain(line) for line in source.lines[start:end] if
                 re.search(r'[\u4e00-\u9fff]', line) and not re.match(r'\s*[<#|!]', line) and
                 len(plain(line)) >= 20 and not re.match(r'\s*图\s*\d', line) and
                 not re.search(r'img/|描述已自动生成|!\[|\]\(', line)]
        if not facts:
            continue
        if re.search(r'接口|函数|[A-Za-z_]{3,}\s*\(', clean) and '参数' in content and re.search(r'返回|返回值', content):
            question, kind, focus = f'{topic}的调用参数和返回值分别是什么？', 'instruction', 'api_contract'
        elif re.search(r'点击|单击|双击|选择|勾选|打开|输入|设置|配置', content) and re.search(
                r'配置|设置|使用|操作|调试|标定|创建|导入|修改|删除|接线|通信|通讯|启动|停止|示教|加载|登录|新建|保存|管理|建模|计算|校准|连接|查询|安装|调用|生成|控制|选择|调整|切换|获取|更新|检测|测试|恢复|编辑|备份|移动|复制|运行|添加|补偿|准备|读取|写入|显示|插入|路径|记录|统计|校验', clean):
            question, kind, focus = f'关于{topic}，应如何操作？', 'procedure', 'section_procedure'
        elif re.search(r'必须|不允许|禁止|仅.*(?:才能|有效)|只有.*才|注意[：:]', content) and re.search(
                r'条件|要求|须知|注意|前提|准备|使用|操作|设定|配置|限制|检查|说明|安全|基础', clean):
            question, kind, focus = f'{topic}有哪些使用条件和注意事项？', 'prerequisite', 'section_conditions'
            facts = [part.strip() for part in re.split(r'(?<=[。；])', content) if re.search(
                r'必须|不允许|禁止|仅.*(?:才能|有效)|只有.*才|注意[：:]', part)]
            if not facts:
                continue
        else:
            continue
        yield source.case(start, end, question, kind, focus, 'section', facts=facts)


def build_candidates(root):
    candidates = []
    sources = []
    for profile in ('4.0', '5.0'):
        corpus = root / f'assets/code-docs.{profile}'
        if not corpus.is_dir():
            raise FileNotFoundError(corpus)
        for path in sorted(corpus.rglob('*_semantics.json')):
            md = path.with_name(path.name.removesuffix('_semantics.json') + '.md')
            if not md.is_file() or not re.search(r'SX3220(?:0[1-9]|1[0-8])', md.name):
                continue
            source = Source(root, md, profile)
            sources.append(dict(markdown=source.relative, profile=profile, category=source.category, sha256=source.sha))
            seen_objects = set()
            for obj in objects(json.loads(path.read_text(encoding='utf-8'))):
                kind = obj.get('type')
                if kind not in {'table_row_group', 'table_row', 'table'} or not obj.get('content'):
                    continue
                identity = (kind, obj.get('line_start'), obj.get('line_end'))
                if identity in seen_objects or not isinstance(identity[1], int) or not isinstance(identity[2], int):
                    continue
                seen_objects.add(identity)
                maker = row_candidates if kind == 'table_row' else instruction_candidates
                candidates.extend(case for case in maker(source, obj) if case)
            candidates.extend(case for case in prose_candidates(source) if case)
            candidates.extend(case for case in section_candidates(source) if case)
    # Source-revision copies and repeated formatting do not inflate sample counts.
    by_question = {}
    for case in sorted(candidates, key=lambda c: digest([c['question'], c['profile'], c['source_md']])):
        normalized = re.sub(r'\s+', '', unicodedata.normalize('NFKC', case['question'])).casefold()
        by_question.setdefault(normalized, case)
    return list(by_question.values()), sources


def select(candidates, count):
    """Round-robin categories and question types, capping diagnosis at 20%."""
    buckets = defaultdict(lambda: defaultdict(list))
    for case in candidates:
        buckets[case['category']][case['kind']].append(case)
    for groups in buckets.values():
        for kind, cases in groups.items():
            groups[kind] = deque(sorted(cases, key=lambda c: digest(c['id'])))
    selected, kinds = [], Counter()
    cursors = Counter()
    while len(selected) < count:
        changed = False
        for category in sorted(buckets):
            groups = buckets[category]
            available = [kind for kind in sorted(groups) if groups[kind] and
                         (kind != 'alarm' or kinds[kind] < count // 5)]
            if not available:
                continue
            kind = available[cursors[category] % len(available)]
            cursors[category] += 1
            selected.append(groups[kind].popleft())
            kinds[kind] += 1
            changed = True
            if len(selected) == count:
                break
        if not changed:
            raise ValueError(f'Only {len(selected)} eligible distinct questions for requested {count}; '
                             f'candidate distribution: {Counter(c["category"] for c in candidates)}')
    return sorted(selected, key=lambda c: (c['category'], c['id']))


def discover_root():
    for parent in (Path.cwd(), *Path(__file__).resolve().parents):
        if (parent / 'assets/code-docs.4.0').is_dir():
            return parent
    raise FileNotFoundError('Use --source-root for the directory containing assets/code-docs.4.0')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[2] /
                        'evaluation_samples/regular_object_rag_3000.json')
    parser.add_argument('--count', type=int, default=3000)
    args = parser.parse_args()
    if args.count < 1:
        parser.error('--count must be positive')
    root = (args.source_root or discover_root()).absolute()
    candidates, sources = build_candidates(root)
    print('Eligible unique candidates:', len(candidates))
    print('Candidate categories:', dict(sorted(Counter(c['category'] for c in candidates).items())))
    print('Candidate kinds:', dict(sorted(Counter(c['kind'] for c in candidates).items())))
    cases = select(candidates, args.count)
    bank = dict(schema_version=1, dataset_type='regular_object_rag_samples', sample_count=len(cases),
                generation=dict(generator='utils/rag/generate_regular_object_samples.py', version=VERSION,
                    method='deterministic_source_extractive_templates', source_profiles=['4.0', '5.0'],
                    reference_notebook='nbs/06.test.rag.object.question_with_category.ipynb',
                    question_language='zh', unique_questions=True, seed='sha256-v1',
                    selection='round_robin_category_and_kind; alarm_maximum_20_percent',
                    eligible_unique_candidates=len(candidates),
                    expected_facts='verbatim whitespace-normalized extracts; reviewer-only',
                    source_paths_relative_to='source_root', original_sources_reverified=True),
                distribution={key: dict(sorted(Counter(c[key] for c in cases).items()))
                              for key in ('category', 'kind', 'profile', 'representation')},
                sources=[dict(row, sample_count=sum(c['source_md'] == row['markdown'] for c in cases))
                         for row in sources], cases=cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    try:
        temporary.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        load_sample_bank(temporary, source_root=root)
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(bank['distribution'], ensure_ascii=False, indent=2))
    print(f'Wrote {len(cases)} questions to {args.output}')


if __name__ == '__main__':
    main()
