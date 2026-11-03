"""数据清洗层：所有文本过滤逻辑集中在这里。

只做纯字符串处理，不依赖 server / db，任何层都可以安全引用。
"""
from __future__ import annotations

import html as html_lib
import re

BOOK_STATUS_RE = re.compile(r"<book_status>.*?</book_status>", re.DOTALL)
NEXT_PROMPT_RE = re.compile(r"<next_prompt>.*?</next_prompt>", re.DOTALL)
IMAGE_TAG_RE = re.compile(r"<image>(.*?)</image>", re.DOTALL)
SAY_RE = re.compile(r"<say>([\s\S]*?)(?:</say>|(?=<say>)|$)", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>[\s\S]*?</\1>", re.I)
BLOCK_TAG_RE = re.compile(
    r"<br\s*/?>|</?(?:p|div|li|ul|ol|h[1-6]|blockquote|section|article|tr|td)[^>]*>",
    re.I)


def normalize_say_blocks(content: str) -> str:
    """模型/旧回复常把整段（含台词）塞进 <say>，或只开不闭合。
    这里把括号内的动作收进 <say>，括号外的文字（台词/旁白）放回正文。"""
    def repl(m):
        inner = m.group(1)
        actions = re.findall(r"（[^（）]*）", inner)
        rest = re.sub(r"（[^（）]*）", "", inner).strip()
        parts = []
        if actions:
            parts.append("<say>" + "\n".join(actions) + "</say>")
        if rest:
            parts.append(rest)
        return "\n".join(parts)
    return SAY_RE.sub(repl, content)


# ---------------------------------------------------------- 剧情选项块
# 模型经常直接把 A/B/C/D 的 markdown 列表写进正文。这里在落库前统一改写：
# 尾部连续的 A. / B. / C. 行 → <next_prompt>，前端按标签渲染成回复按钮。
_OPTION_LINE_RE = re.compile(
    r"^\s*(?:[-*•·>#]{1,3}\s*)*([A-Da-d]|[1-9])\s*[.、)）:：]\s*(.+?)\s*$")
_OPTION_TAIL_RE = re.compile(r"^(?:请)?(?:你)?(?:做出|作出)?(?:选择|选项)[：:\s]*$")


def _is_option_noise(line: str) -> bool:
    """选项块前后的引导语（「请做出你的选择：」「下一步选项」）—— 不算正文"""
    t = str(line or "").strip().strip("*_`>#[]【】").strip()
    if not t:
        return True
    if _OPTION_TAIL_RE.match(t):
        return True
    bare = t.rstrip("：: ")
    if len(bare) <= 28 and any(k in bare for k in ("选择", "选项")):
        return True
    return len(bare) <= 16 and bare.endswith(("决定", "行动", "回复"))


def wrap_next_prompts(text: str) -> str:
    """把回复尾部的 A/B/C/D（或 1./2./3.）选项块包进 <next_prompt>。

    已经有标签 → 原样返回；选项少于 2 个、标号不连续（A,B,C / 1,2,3）→
    认为只是正文里的列表，不动它。
    """
    s = str(text or "")
    if not s.strip() or "<next_prompt" in s.lower():
        return s
    # 状态栏可能排在选项前、也可能排在后（模型两种顺序都写过）：先整块摘出来，
    # 否则「尾部选项块」会被状态栏挡住，选项就永远包不进 <next_prompt>。
    status_blocks = re.findall(r"<book_status[^>]*>[\s\S]*?</book_status>", s, re.I)
    head_s = re.sub(r"<book_status[^>]*>[\s\S]*?</book_status>", "", s, flags=re.I)
    lines = head_s.rstrip().split("\n")
    i = len(lines) - 1
    # 先跳过选项块后面的引导语 / 空行（「请做出你的选择：」之类）
    while i >= 0 and _is_option_noise(lines[i]):
        i -= 1
    start = None
    opts: list[str] = []
    while i >= 0:
        line = lines[i].strip()
        if _is_option_noise(line):
            i -= 1
            continue
        m = _OPTION_LINE_RE.match(line)
        if not m:
            break
        body = re.sub(r"(\*\*|__|\*|_|`)+", "", m.group(2))
        body = " ".join(body.split())
        if not body:
            break
        opts.append(f"{m.group(1).upper()}. {body}")
        start = i
        i -= 1
    if start is None or len(opts) < 2:
        return s
    opts.reverse()
    labels = [o[0] for o in opts]
    letters = [chr(ord("A") + k) for k in range(len(labels))]
    numbers = [str(k + 1) for k in range(len(labels))]
    if labels not in (letters, numbers):
        return s
    visible = "\n".join(lines[:start]).rstrip()
    while visible and _is_option_noise(visible.split("\n")[-1]):
        visible = "\n".join(visible.split("\n")[:-1]).rstrip()
    if not visible.strip():
        return s
    np_block = "\n\n<next_prompt>\n" + "\n".join(opts) + "\n</next_prompt>"
    status_text = "".join(f"\n\n{blk.strip()}" for blk in status_blocks)
    # 保持原来的先后顺序（状态栏在前就仍在前）：流式已发出去的半截内容
    # 才不会被重排，避免收尾时整段 clear_content 闪一下
    first_status_at = s.lower().find("<book_status")
    opt_at = s.find(visible) + len(visible) if visible else -1
    if (status_blocks and first_status_at != -1 and opt_at != -1
            and first_status_at < opt_at):
        return visible + status_text + np_block
    return visible + np_block + status_text


def stream_hold_start(text: str) -> int:
    """流式下发边界：结尾还没定稿的部分从这里开始。

    两类要攒住：① 最后一行（可能只收到半个词）；② 尾部的选项块（要整体
    改写成 <next_prompt>，中途吐出去就收不回来了）。
    """
    lines = str(text or "").split("\n")
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    hold = offsets[-1]
    tail_len = len(lines[-1])
    if tail_len > 60:            # 这么长的半行不可能是选项，先发出去（只留个尾巴）
        hold = min(len(text), offsets[-1] + tail_len - 20)
    for i in range(len(lines) - 2, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        low = line.lower()
        # 状态栏整块也可能排在选项前面/后面，一并跳过，继续往前找到选项块开头
        if (_OPTION_LINE_RE.match(line) or _is_option_noise(line)
                or low.startswith(("<book_status", "</book_status", "<item", "</item"))):
            hold = offsets[i]
            continue
        break
    return hold


def clean_description_html(html_text: str) -> str:
    """角色详细介绍是作者写的 HTML；拼进 system prompt 前要转成纯文本。

    保留段落分隔，去掉脚本/样式与全部标签，并还原 HTML 实体。
    """
    text = str(html_text or "")
    if not text:
        return ""
    text = SCRIPT_STYLE_RE.sub("", text)
    text = BLOCK_TAG_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def clean_history_for_llm(raw_content: str) -> str:
    """清洗发给大模型的历史记录：剥离状态栏、选项块与图片 ID，只留动作与对话。
    仅用于拼 prompt，库里原文（渲染要用的 <book_status>/<next_prompt>）不动。"""
    if not raw_content:
        return ""
    # 图片替换成纯文本标识，防止模型复读历史里的图片 ID
    content = IMAGE_TAG_RE.sub("[发送了一张图片]", str(raw_content))
    content = BOOK_STATUS_RE.sub("", content)
    content = NEXT_PROMPT_RE.sub("", content)
    content = normalize_say_blocks(content)
    return content.strip()


def clean_for_storage(raw_text: str) -> str:
    """落库前把 LLM 原始输出清洗成「喂模型用的纯文本」，写进 content_llm 列。

    展示用的原文仍留在 content（前端要渲染状态栏/选项/CG），
    所以下一轮拼 Prompt 直接读 content_llm，不再现算。
    """
    if not raw_text:
        return ""
    text = IMAGE_TAG_RE.sub("[发送了一张图片]", str(raw_text))
    text = BOOK_STATUS_RE.sub("", text)
    text = NEXT_PROMPT_RE.sub("", text)
    text = re.sub(r"</?say>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
