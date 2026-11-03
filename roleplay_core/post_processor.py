"""响应后处理器：把 LLM 原始输出变成前端可直接渲染的结构。

- 拦截 <image>：CG ID → 真实 URL，编造的 ID 丢弃，图片统一挪到正文最前
- 提取 <book_status>：拆成 [{name, value}]，前端据此重建原生标签
- 提取 <next_prompt>：选项列表
- display_text 去掉状态栏/选项（前端用结构化数据重建），tagged_text 保留供落库
"""
import re
from typing import Callable, Dict, List, Optional

STATUS_BLOCK_RE = re.compile(r"<book_status[^>]*>([\s\S]*?)</book_status>", re.I)
NEXT_PROMPT_BLOCK_RE = re.compile(r"<next_prompt[^>]*>([\s\S]*?)</next_prompt>", re.I)
IMAGE_TAG_RE = re.compile(r"<image>(.*?)</image>", re.DOTALL)
IMAGE_CAPTURE_RE = re.compile(r"<image>[\s\S]*?</image>")
ITEM_RE = re.compile(r"<item>([\s\S]*?)</item>", re.I)
TAG_RE = re.compile(r"<[^>]+>")
OPTION_RE = re.compile(r"^[A-Za-z]\s*[、.．]\s*\S")


FAVOR_MIN, FAVOR_MAX = -100, 100
SAN_MIN, SAN_MAX = 0, 100
FAVOR_NAME_RE = re.compile(r"好感|亲密度|信任|favor", re.I)
SAN_NAME_RE = re.compile(r"san", re.I)


def _bounds(name: str, schema: Optional[Dict]) -> tuple[Optional[int], Optional[int]]:
    """属性上下限：优先角色卡 schema，其次内建的好感度 / SAN 规则。"""
    cfg = (schema or {}).get(str(name or "").strip())
    if isinstance(cfg, dict) and ("min" in cfg or "max" in cfg):
        return cfg.get("min"), cfg.get("max")
    if SAN_NAME_RE.search(str(name or "")):
        return SAN_MIN, SAN_MAX
    if FAVOR_NAME_RE.search(str(name or "")):
        return FAVOR_MIN, FAVOR_MAX
    return None, None


def _clamp_num(name: str, num: int, schema: Optional[Dict] = None) -> int:
    """按角色卡 schema 截断；没声明上下限的属性原样返回。"""
    lo, hi = _bounds(name, schema)
    if lo is not None:
        num = max(int(lo), num)
    if hi is not None:
        num = min(int(hi), num)
    return num


def _clamp_value_text(name: str, value: str,
                      schema: Optional[Dict] = None) -> str:
    """只改第一个数字，保留「/100」这类后缀。"""
    m = re.search(r"-?\d+", str(value or ""))
    if not m:
        return value
    num = int(m.group(0))
    clamped = _clamp_num(name, num, schema)
    if clamped == num:
        return value
    return value[:m.start()] + str(clamped) + value[m.end():]


def _plain(text) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", str(text or ""))).strip()


def _parse_status(text: str,
                  schema: Optional[Dict] = None) -> List[Dict[str, str]]:
    """解析状态栏里的每条 <item>：属性名与数值按角色设定，不写死。"""
    m = STATUS_BLOCK_RE.search(str(text or ""))
    if not m:
        return []
    items: List[Dict[str, str]] = []
    for raw_item in ITEM_RE.findall(m.group(1)):
        plain = _plain(raw_item)
        if not plain:
            continue
        parts = re.split(r"[：:]", plain, maxsplit=1)
        if len(parts) == 2:
            items.append({"name": parts[0].strip(),
                          "value": _clamp_value_text(parts[0], parts[1].strip(),
                                                     schema)})
        else:
            items.append({"name": "", "value": plain})
    return items


def _clamp_status_text(text: str, schema: Optional[Dict] = None) -> str:
    """把状态栏里越界的数字就地改回合法值（落库/展示都以这份文本为准）。"""
    def fix_item(m):
        body = m.group(1)
        plain = _plain(body)
        parts = re.split(r"[：:]", plain, maxsplit=1)
        name = parts[0] if len(parts) == 2 else ""
        mnum = re.search(r"-?\d+", body)
        if not mnum:
            return m.group(0)
        num = int(mnum.group(0))
        clamped = _clamp_num(name, num, schema)
        if clamped == num:
            return m.group(0)
        return "<item>" + body[:mnum.start()] + str(clamped) + body[mnum.end():] + "</item>"

    return ITEM_RE.sub(fix_item, str(text or ""))


def _parse_options(text: str) -> List[str]:
    """解析 <next_prompt> 里的选项行（保留「A、」这样的标号）。"""
    m = NEXT_PROMPT_BLOCK_RE.search(str(text or ""))
    if not m:
        return []
    return [line.strip() for line in m.group(1).splitlines()
            if line.strip() and OPTION_RE.match(line.strip())]


def _cg_image_url(img: dict) -> str:
    for k in ("url", "imageURL", "imageUrl"):
        v = str(img.get(k) or "").strip()
        if v:
            return v
    return ""


def _image_index(cg_data: Optional[Dict]) -> Dict[str, dict]:
    by_id: Dict[str, dict] = {}
    for img in ((cg_data or {}).get("images") or []):
        if not isinstance(img, dict):
            continue
        for k in ("id", "imageId"):
            pid = str(img.get(k) or "").strip()
            if pid:
                by_id.setdefault(pid, img)
    return by_id


def _map_images(text: str, cg_data: Optional[Dict]) -> tuple[str, bool]:
    """把模型点的 <image>ID</image> 换成真实地址；编造的 ID 直接抹掉。"""
    by_id = _image_index(cg_data)
    if not by_id:
        return text, False

    def repl(m):
        img = by_id.get(m.group(1).strip())
        if not img:
            return ""
        url = _cg_image_url(img)
        return f"<image>{url}</image>" if url else ""

    text = IMAGE_TAG_RE.sub(repl, text)
    return text, "<image>" in text


def hoist_images(text: str) -> str:
    """把图片标签统一挪到正文最前面（前端第一屏就能看到画面）。"""
    tags = IMAGE_CAPTURE_RE.findall(str(text or ""))
    if not tags:
        return text
    body = IMAGE_CAPTURE_RE.sub("", text)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    head = "\n".join(tags)
    return head + ("\n" + body if body else "")


def parse_status(text: str,
                 schema: Optional[Dict] = None) -> List[Dict[str, str]]:
    """流式期间就取状态栏（闭合的 <book_status> 才有效），给 status_update 事件用。"""
    return _parse_status(text, schema)


def status_dict(items: List[Dict[str, str]]) -> Dict[str, object]:
    """items → 通用状态字典 {属性名: 数值}，不关心有哪些属性。"""
    out: Dict[str, object] = {}
    for it in items or []:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        raw = str(it.get("value") or "").strip()
        m = re.search(r"-?\d+", raw)
        out[name] = int(m.group(0)) if m else raw
    return out


def process_output(raw_text: str, cg_list: Optional[Dict] = None,
                   enable_cg: bool = True, current_user_msg: str = "",
                   matcher: Optional[Callable] = None,
                   strip_ui: bool = True,
                   schema: Optional[Dict] = None) -> Dict:
    """原始输出 → 展示态。

    - enable_cg=False：剥离所有 <image>，本次不展示插图
    - 有 CG 库：ID 映射成真实地址；正文没点图时按权重兜底补一张
    - 图片统一置顶；strip_ui=True 时 display_text 去掉状态栏/选项，
      改由前端用 status/options 数据重建 DOM；tagged_text 保留标签供落库
    """
    raw = _clamp_status_text(str(raw_text or ""), schema)
    status = _parse_status(raw, schema)
    options = _parse_options(raw)

    if not enable_cg:
        tagged = IMAGE_TAG_RE.sub("", raw)
        has_image = False
    else:
        tagged, has_image = _map_images(raw, cg_list)
        if not has_image and cg_list and matcher:
            images = [x for x in (cg_list.get("images") or []) if isinstance(x, dict)]
            matched = matcher(f"{current_user_msg or ''} {tagged}", images,
                              cg_list.get("weights")) if images else None
            if matched:
                url = _cg_image_url(matched)
                if url:
                    tagged = f"<image>{url}</image>\n" + tagged
                    has_image = True

    tagged = hoist_images(tagged)
    images = [str(u).strip() for u in IMAGE_TAG_RE.findall(tagged) if str(u).strip()]

    display = tagged
    if strip_ui:
        display = STATUS_BLOCK_RE.sub("", display)
        display = NEXT_PROMPT_BLOCK_RE.sub("", display)
        display = re.sub(r"\n{3,}", "\n\n", display).strip()

    return {"display_text": display, "tagged_text": tagged, "has_image": has_image,
            "status": status, "options": options, "images": images}
