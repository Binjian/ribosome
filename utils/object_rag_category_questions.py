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
