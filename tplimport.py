#!/usr/bin/env python3
"""
tplimport.py — 第三方模板 JSON 的通用适配（行业通用格式 → MediaGen 配方）

别人做的模板 JSON 长短不一，这里做三件事：
  1. 认来源：URL / 粘贴文本 / 文件内容（服务端拉取，不依赖浏览器）
  2. 认格式：原生目录 categories → 模板清单 templates|items|recipes|list
             → 单模板对象 → 通用模板格式 missai.template/v1
             → 常见平台导出（SD WebUI 参数、ComfyUI API 工作流）
  3. 认字段：字段名按行业别名映射（id|key|slug、title|name、inputs|fields、
             enum|choices→options、default|value→defaultValue …）

适配完统一交给 mediagen.normalize() 裁剪，保证 ai-creator 前端认得出、不静默丢弃。

推荐对外发布的通用模板格式（字段名取行业惯用写法）：

    {
      "schema": "missai.template/v1",
      "id": "portrait-studio",
      "title": "证件照生成",
      "category": {"key": "portrait", "name": "人像写真"},
      "description": "白底/蓝底证件照",
      "author": "someone",
      "version": 1,
      "output": {"mediaType": "image", "count": 1},
      "model": {"name": "gpt-image-1"},
      "params": {
        "size": "1024x1536",
        "aspectRatios": {"1:1": "1024x1024", "3:4": "1024x1536"}
      },
      "prompt": {
        "template": "{subject}，{style}，{lighting}，高清细节",
        "negative": "低质量, 模糊, 变形"
      },
      "inputs": [
        {"key": "subject", "label": "画面主体", "type": "text", "required": true},
        {"key": "style", "label": "风格", "type": "select", "default": "ink",
         "options": [{"value": "ink", "label": "水墨"}, {"value": "oil", "label": "油画"}]}
      ]
    }

prompt.template 里的 {字段key} 会被用户填的值替换（没填就留空），
所以同一份模板换到别的平台上也能直接用。
"""

import hashlib
import json
import re
import urllib.request
from urllib.parse import urlparse

import mediagen

MAX_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT = 10
DEFAULT_CATEGORY = {"key": "imported", "name": "导入模板"}

LIST_KEYS = ("templates", "items", "recipes", "list", "entries", "presets", "data")
FIELD_LIST_KEYS = ("inputs", "fields", "form", "controls", "variables", "parameters")

TYPE_ALIAS = {
    "text": "text", "string": "text", "input": "text", "str": "text",
    "number": "text", "integer": "text", "float": "text", "step": "text",
    "slider": "text", "range": "text", "resolution": "text",
    "textarea": "textarea", "longtext": "textarea", "multiline": "textarea",
    "text-area": "textarea", "rich": "textarea", "prompt": "textarea",
    "select": "select", "enum": "select", "choice": "select", "choices": "select",
    "dropdown": "select", "radio": "select", "option": "select", "listbox": "select",
}
BOOL_TYPES = ("bool", "boolean", "switch", "toggle", "checkbox")


# --------------------------------------------------------------- 通用取值

def _first(obj, *names, default=None):
    if not isinstance(obj, dict):
        return default
    for name in names:
        if name in obj:
            value = obj[name]
            if value is not None and value != "":
                return value
    return default


def _str(value, default="") -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value if isinstance(value, str) else default


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _slug(value, fallback: str = "tpl") -> str:
    """recipeKey / 字段 key 只允许 [A-Za-z0-9][A-Za-z0-9._:-]*（前端正则）"""
    text = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "").strip())
    text = text.strip("-._:")
    if text and mediagen.KEY_RE.match(text):
        return text
    digest = hashlib.md5(str(value or fallback).encode("utf-8")).hexdigest()[:8]
    return f"{fallback}-{digest}"


def _ratio_to_size(ratio: str) -> str:
    try:
        parts = (str(ratio).replace("：", ":").replace("×", ":")
                 .replace("X", ":").replace("x", ":")).split(":")
        a, b = float(parts[0]), float(parts[1])
    except (IndexError, TypeError, ValueError):
        return ""
    if a <= 0 or b <= 0:
        return ""
    # 常见比例优先用绘图模型的标称尺寸（gpt-image / DALL·E 只认这几个）
    for ra, rb, size in ((1, 1, "1024x1024"), (2, 3, "1024x1536"), (3, 2, "1536x1024")):
        if abs(a / b - ra / rb) < 0.005:
            return size
    base = 1024
    if a >= b:
        return f"{base}x{max(256, int(round(base * b / a / 8) * 8))}"
    return f"{max(256, int(round(base * a / b / 8) * 8))}x{base}"


# --------------------------------------------------------------- 字段

def _options(value) -> list:
    out, seen = [], set()
    if isinstance(value, dict):
        pairs = [{"value": k, "label": v} for k, v in value.items()]
    elif isinstance(value, list):
        pairs = value
    else:
        return out
    for item in pairs:
        if isinstance(item, dict):
            raw = _first(item, "value", "key", "id", "name", "label")
            label = _first(item, "label", "title", "name", "value", default=raw)
        else:
            raw = label = item
        val, text = _str(raw).strip(), _str(label).strip()
        if not val or not text or val in seen:
            continue
        seen.add(val)
        out.append({"value": val, "label": text[:40]})
    return out[:40]


def _adapt_field(raw, index: int, notes: list) -> dict | None:
    if isinstance(raw, str):
        key = _slug(raw, f"field{index + 1}")
        return {"key": key, "label": raw.strip()[:60] or key, "type": "text"}
    if not isinstance(raw, dict):
        return None

    key = _slug(_first(raw, "key", "name", "id", "field", "slug",
                        default=f"field{index + 1}"), f"field{index + 1}")
    label = _str(_first(raw, "label", "title", "name", "displayName", "desc",
                        "description", default=key)).strip() or key
    kind_raw = _str(_first(raw, "type", "inputType", "widget", "control",
                           "format", default="text")).strip().lower()
    if kind_raw in BOOL_TYPES:
        kind, options = "select", [{"value": "yes", "label": "是"}, {"value": "no", "label": "否"}]
    else:
        kind = TYPE_ALIAS.get(kind_raw, "text")
        options = _options(_first(raw, "options", "enum", "choices", "items", "values"))
    if kind == "select" and not options:
        kind = "text"                     # 没选项的 select 会被前端整条丢掉
        notes.append(f"{label} 没有可选项，已降级为文本框")

    field = {"key": key, "type": kind, "label": label[:60],
             "required": bool(_first(raw, "required", "isRequired", "mandatory", default=False))}
    placeholder = _str(_first(raw, "placeholder", "hint", "tip", "example")).strip()
    if placeholder:
        field["placeholder"] = placeholder[:120]
    if kind == "select":
        field["options"] = options
        default = _str(_first(raw, "defaultValue", "default", "value", "initial", "preset")).strip()
        if default not in [o["value"] for o in options]:
            default = options[0]["value"]
        field["defaultValue"] = default
    else:
        default = _str(_first(raw, "defaultValue", "default", "value", "initial", "preset")).strip()
        if default:
            field["defaultValue"] = default
    return field


def _collect_fields(item: dict, notes: list, has_prompt: bool = False) -> list:
    raw_fields = _dict(item.get("schema")).get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raw_fields = None
        for name in FIELD_LIST_KEYS:
            value = item.get(name)
            if isinstance(value, list) and value:
                raw_fields = value
                break
    fields, seen = [], set()
    for index, raw in enumerate(_list(raw_fields)[:40]):
        field = _adapt_field(raw, index, notes)
        if not field or field["key"] in seen:
            continue
        seen.add(field["key"])
        fields.append(field)
    if not fields:
        # 没有表单字段也要能用：给一个描述框；提示词已内置就不强求填写
        fields.append({"key": "prompt", "type": "textarea",
                       "label": "补充描述" if has_prompt else "画面描述",
                       "required": not has_prompt,
                       "placeholder": ("可留空，默认用模板自带提示词" if has_prompt
                                       else "描述你想生成的画面")})
        notes.append("没有表单字段，已补一个" + ("「补充描述」（可留空）" if has_prompt
                                                else "「画面描述」输入框"))
    return fields


def _ratio_map(item: dict) -> dict:
    params = _dict(item.get("params"))
    raw = _first(params, "aspectRatios", "ratios", "sizeByRatio", "sizes")
    if raw in (None, "", []):
        raw = _first(item, "aspectRatios", "ratios", "sizeByRatio")
    out = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            ratio, size = _str(key).strip(), _str(value).strip().lower()
            if ratio and "x" in size:
                out[ratio] = size
    elif isinstance(raw, list):
        for one in raw:
            if isinstance(one, dict):
                ratio = _str(_first(one, "ratio", "key", "label", "value")).strip()
                size = _str(_first(one, "size", "resolution", "value")).strip().lower()
            else:
                ratio, size = _str(one).strip(), ""
            if not ratio:
                continue
            size = size if "x" in size else _ratio_to_size(ratio)
            if "x" in size:
                out[ratio] = size
    if not out:
        # 单个固定比例：params.aspectRatio / ratio（证件照、头像这类模板常见）
        one = _str(_first(params, "aspectRatio", "ratio")).strip() \
            or _str(_first(item, "aspectRatio", "defaultRatio")).strip()
        if one:
            size = _str(_first(params, "size", "resolution")).strip().lower()
            size = size if "x" in size else _ratio_to_size(one)
            if "x" in size:
                out[one] = size
    return out


# --------------------------------------------------------------- 常见平台

def _is_comfy(item: dict) -> bool:
    if isinstance(item.get("nodes"), list) and "links" in item:
        return True
    return any(isinstance(v, dict) and "class_type" in v for v in item.values())


def _from_comfy(item: dict, notes: list) -> dict:
    """ComfyUI 工作流 → 提示词 / 负向词 / 模型名（尽力提取）"""
    local = {}
    nodes = item.get("nodes")
    if isinstance(nodes, list):
        notes.append("ComfyUI 界面格式（nodes/links）不便直接解析，"
                     "建议在 ComfyUI 里用 Save (API Format) 导出后再导入")
        for node in nodes:
            if not isinstance(node, dict):
                continue
            values = [v for v in _list(node.get("widgets_values")) if isinstance(v, str)]
            ctype = _str(node.get("type"))
            title = _str(_dict(node.get("_meta")).get("title"))
            if "CheckpointLoader" in ctype:
                cand = [v for v in values if "/" in v or "." in v]
                if cand:
                    local["model"] = cand[0]
            elif "CLIPTextEncode" in ctype:
                text = max(values, key=len) if values else ""
                if not text.strip():
                    continue
                if "负" in title.lower() or "negative" in title.lower():
                    local.setdefault("negative", text.strip())
                else:
                    local.setdefault("promptPrefix", text.strip() + "，")
        return local

    positives, negatives, model = [], [], ""
    for node in item.values():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        ctype = _str(node.get("class_type"))
        inputs = _dict(node.get("inputs"))
        title = _str(_dict(node.get("_meta")).get("title")).lower()
        if "CLIPTextEncode" in ctype:
            text = inputs.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if "负" in title or "negative" in title:
                negatives.append(text.strip())
            else:
                positives.append(text.strip())
        elif "CheckpointLoader" in ctype:
            model = _str(_first(inputs, "ckpt_name", "ckpt_name_1", "unet_name")).strip()
        elif "UNETLoader" in ctype:
            model = model or _str(_first(inputs, "unet_name")).strip()
    if positives:
        local["promptPrefix"] = positives[0] + "，"
    if negatives:
        local["negative"] = negatives[0]
    if model:
        local["model"] = model
    if not positives and not model:
        notes.append("工作流里没找到正/负向提示词节点，只导入了结构")
    return local


def _from_sd(item: dict) -> dict:
    """SD WebUI / A1111 参数片段 → 负向词与尺寸"""
    local = {}
    negative = _str(_first(item, "negative_prompt", "negativePrompt", "negative")).strip()
    if negative:
        local["negative"] = negative
    width, height = item.get("width"), item.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)) and width and height:
        size = f"{int(width)}x{int(height)}"
        local["size"] = size
        local["sizeByRatio"] = {"1:1": size}
    return local


# --------------------------------------------------------------- 配方适配

def _collect_local(item: dict, notes: list) -> dict:
    local = {k: v for k, v in _dict(item.get("local")).items()
             if isinstance(v, (str, int, float, bool, dict, list))}

    prompt = item.get("prompt")
    cfg = _dict(prompt)
    if cfg:
        template = _str(_first(cfg, "template", "text", "value", "body")).strip()
        prefix = _str(_first(cfg, "prefix", "prepend", "before")).strip()
        suffix = _str(_first(cfg, "suffix", "append", "after")).strip()
        negative = _str(_first(cfg, "negative", "negativePrompt", "negative_prompt", "avoid")).strip()
    else:
        template = _str(prompt).strip() if isinstance(prompt, str) else ""
        template = template or _str(_first(item, "promptTemplate", "prompt_template")).strip()
        prefix = _str(_first(item, "promptPrefix", "prompt_prefix", "stylePrefix")).strip()
        suffix = _str(_first(item, "promptSuffix", "prompt_suffix")).strip()
        negative = _str(_first(item, "negativePrompt", "negative_prompt", "negative")).strip()

    templated = bool(re.search(r"\{[A-Za-z0-9._:-]+\}", template))
    if templated:
        local["promptTemplate"] = template
        if prefix:
            local["promptPrefix"] = prefix
    elif template:
        local["promptPrefix"] = prefix + template
    elif prefix:
        local["promptPrefix"] = prefix
    if suffix:
        local["promptSuffix"] = suffix
    if negative:
        local["negative"] = negative

    mapping = _dict(_first(cfg, "fieldMap", "map", "labels", "optionPrompts")) \
        or _dict(_first(item, "fieldPrompt", "fieldPrompts"))
    if mapping:
        clean = {str(k): {str(vk): _str(vv) for vk, vv in vm.items() if _str(vv)}
                 for k, vm in mapping.items() if isinstance(vm, dict)}
        if clean:
            local["fieldPrompt"] = clean

    model = _first(item, "model", "checkpoint", "baseModel", "modelName", "baseModelName")
    if isinstance(model, dict):
        local["model"] = _str(_first(model, "name", "model", "id", "modelKey")).strip()
        rate = _first(model, "rateId", "providerId", "endpoint")
        if rate not in (None, ""):
            local["rateId"] = _int(rate, 0)
    elif _str(model).strip():
        local.setdefault("model", _str(model).strip())

    params = _dict(item.get("params"))
    count = _int(_first(params, "count", "n", "numImages", "quantity",
                        default=_first(item, "count", "n", default=1)), 1)
    if count < 1 or count > 4:
        notes.append(f"批量数量 {count} 已收敛到 1~4")
    local["count"] = max(1, min(4, count))

    size = _str(_first(params, "size", "resolution", "imageSize")).strip() \
        or _str(_first(item, "size", "resolution", "defaultSize", "imageSize")).strip()
    if "x" in size.lower() or "*" in size or "×" in size:
        local["size"] = size.lower().replace("*", "x").replace("×", "x")
    return local


def _cover(item: dict) -> str:
    url = _str(_first(item, "coverUrl", "cover", "cover_url", "thumbnail", "thumb",
                      "preview", "image", "icon")).strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("/", "http://", "https://", "data:")):
        return url
    return ""


def _looks_like_template(item) -> bool:
    if not isinstance(item, dict) or not item:
        return False
    if _is_comfy(item):
        return True
    keys = set(item)
    if keys & {"schema", "inputs", "fields", "form", "prompt", "promptTemplate",
               "workflow", "negative_prompt"}:
        return True
    return bool(keys & {"name", "title", "recipeKey", "id"}) and bool(
        keys & {"model", "params", "steps", "size", "version", "description"})


def _adapt_recipe(item, hint, ctx: dict):
    if not isinstance(item, dict) or not item:
        return None, hint

    # 已经是本机配方（带 recipeKey + submitProtocol + schema）：原样放行，只补默认值
    schema = _dict(item.get("schema"))
    if item.get("recipeKey") and item.get("submitProtocol") and schema:
        recipe = dict(item)
        recipe.setdefault("name", recipe.get("recipeKey"))
        recipe.setdefault("version", 1)
        recipe.setdefault("sortOrder", 0)
        recipe.setdefault("description", "")
        recipe.setdefault("coverUrl", "")
        recipe.setdefault("outputMediaType", "image")
        recipe.setdefault("price", {"points": 0, "pricingMode": "fixed", "isEstimate": False})
        recipe.setdefault("schema", {"fields": [], "mediaSlots": []})
        key = _dedupe_key(str(recipe["recipeKey"]), ctx)
        recipe["recipeKey"] = key
        return recipe, hint

    name = _str(_first(item, "name", "title", "displayName", "display_name",
                       "label", "templateName")).strip()
    raw_key = _first(item, "recipeKey", "id", "key", "slug", "templateId", "uid", default=name)
    notes: list = []

    cat = item.get("category")
    if isinstance(cat, dict):
        hint = {"key": _slug(_first(cat, "key", "id", "name", default=DEFAULT_CATEGORY["key"]), "cat"),
                "name": _str(_first(cat, "name", "title", "label")).strip()
                        or DEFAULT_CATEGORY["name"]}
    elif isinstance(cat, str) and cat.strip():
        cat_name = cat.strip()[:40]
        # 分类名和父级一致就沿用父级 key，别把 categories[].key 弄丢
        if not hint or hint.get("name") != cat_name:
            hint = {"key": _slug(cat_name, "cat"), "name": cat_name}

    local = _collect_local(item, notes)
    if _is_comfy(item):
        local.update(_from_comfy(item, notes))
        if not name:
            base = _str(local.get("model") or "").strip()
            base = base.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            name = f"ComfyUI · {base}" if base else "ComfyUI 工作流"
            raw_key = name
    if any(k in item for k in ("negative_prompt", "steps", "sampler_name", "cfg_scale")):
        local.update(_from_sd(item))

    name = name or _str(raw_key).strip() or "未命名模板"
    key = _slug(raw_key or name, "tpl")
    fields = _collect_fields(item, notes,
                             bool(local.get("promptTemplate") or local.get("promptPrefix")))
    slots = [s for s in _list(schema.get("mediaSlots")) if isinstance(s, dict)]
    if any(s.get("required") for s in slots):
        notes.append("模板需要上传参考图，本地模式暂不支持，只能走文生图")
    seen = {f["key"] for f in fields}

    ratios = _ratio_map(item)
    if ratios:
        local["sizeByRatio"] = ratios
        if len(ratios) == 1:
            local["size"] = next(iter(ratios.values()))       # 固定比例，不用再选
        elif "aspectRatio" not in seen:
            fields.insert(0, {"key": "aspectRatio", "type": "select", "label": "画面比例",
                              "options": [{"value": k, "label": k} for k in ratios],
                              "defaultValue": next(iter(ratios))})
            seen.add("aspectRatio")
    local.setdefault("size", "1024x1024")

    media_type = _str(_first(item, "outputMediaType", "mediaType",
                             default=_dict(item.get("output")).get("mediaType"))).strip()
    if media_type != "image" and media_type:
        notes.append(f"模板声明输出 {media_type}，本地只出图，已按 image 处理")

    points = 0
    raw_price = item.get("price")
    raw_points = _first(_dict(raw_price), "points", "credits") if isinstance(raw_price, dict) else raw_price
    declared = _int(raw_points, 0)
    if declared > 0:
        if ctx["keep_price"]:
            points = declared
            notes.append(f"保留模板积分要求 {declared}（余额不足时前端会拦住）")
        else:
            notes.append(f"模板积分要求 {declared} 已置 0（本地生成不扣积分）")

    recipe = {
        "recipeKey": key,
        "name": name[:60],
        "categoryKey": (hint or {}).get("key") or DEFAULT_CATEGORY["key"],
        "description": _str(_first(item, "description", "desc", "summary",
                                   "intro", "subtitle")).strip()[:200],
        "coverUrl": _cover(item),
        "version": max(1, _int(_first(item, "version", "rev", "revision", default=1), 1)),
        "sortOrder": _int(_first(item, "sortOrder", "order", "sort", default=0), 0),
        "outputMediaType": "image",
        "submitProtocol": "aiMediaRecipe",
        "price": {"points": points, "pricingMode": "fixed", "isEstimate": False},
        "schema": {"fields": fields, "mediaSlots": slots},
        "local": local,
    }
    author = _str(_first(item, "author", "creator", "by", "owner")).strip()
    if author:
        recipe["local"]["author"] = author[:40]

    recipe["recipeKey"] = _dedupe_key(key, ctx)
    prefix = f"{recipe['name']}：" if notes else ""
    ctx["warnings"].extend(prefix + n for n in notes)
    return recipe, hint


def _dedupe_key(key: str, ctx: dict) -> str:
    """同一次导入里 key 不能重复（前端会静默丢弃重的）"""
    unique = key
    suffix = 2
    while unique in ctx["used"]:
        unique = f"{key}-{suffix}"
        suffix += 1
    if unique != key:
        ctx["warnings"].append(f"模板 key {key} 重复，已改名为 {unique}")
    ctx["used"].add(unique)
    return unique


# --------------------------------------------------------------- 结构摊平

def _flatten(raw):
    """任意结构 → ([(item, 分类提示)], 格式名, 提示)"""
    notes: list = []
    if isinstance(raw, list):
        return [(x, None) for x in raw], "list", notes
    if not isinstance(raw, dict):
        return [], "unknown", notes

    cats = raw.get("categories")
    if isinstance(cats, list):
        items = []
        for cat in cats:
            if not isinstance(cat, dict):
                continue
            hint = {"key": _slug(_first(cat, "key", "id", "name", default=DEFAULT_CATEGORY["key"]), "cat"),
                    "name": _str(_first(cat, "name", "title", "label")).strip()
                            or DEFAULT_CATEGORY["name"]}
            inner = None
            for name in LIST_KEYS:
                if isinstance(cat.get(name), list):
                    inner = cat[name]
                    break
            for one in inner or []:
                items.append((one, hint))
        return items, "catalog", notes

    for name in LIST_KEYS:
        value = raw.get(name)
        if isinstance(value, list):
            return [(x, None) for x in value], f"{name}[]", notes
        if name == "data" and isinstance(value, dict):
            items, fmt, inner_notes = _flatten(value)
            return items, f"data.{fmt}", notes + inner_notes

    return [(raw, None)], "single", notes


def _read_source(payload, warnings: list):
    if isinstance(payload, list):
        return payload, "text"
    if not isinstance(payload, dict):
        return None, "unknown"

    url = _str(_first(payload, "url", "sourceUrl", "source_url")).strip()
    if url:
        return _fetch(url, warnings), "url"

    # json 里直接塞对象/数组也认（前端不一定要先序列化成字符串）
    inner = payload.get("json")
    if isinstance(inner, (dict, list)):
        return inner, "text"

    text = _str(_first(payload, "json", "text", "content", "body")).strip()
    if text:
        try:
            return json.loads(text), "text"
        except ValueError as exc:
            warnings.append(f"JSON 解析失败：{exc}")
            return None, "text"

    for name in ("catalog", "template", "templates"):
        value = payload.get(name)
        if isinstance(value, (dict, list)):
            return value, "text"
    return payload, "text"


def _fetch(url: str, warnings: list):
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        warnings.append("只支持 http/https 链接")
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MissAI-Local/1.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            blob = resp.read(MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 —— 网络问题不抛给前端
        warnings.append(f"下载模板失败：{exc}")
        return None
    if len(blob) > MAX_BYTES:
        warnings.append("模板文件超过 4MB，已拒绝")
        return None
    try:
        return json.loads(blob.decode("utf-8-sig", "replace"))
    except ValueError as exc:
        warnings.append(f"下载到的内容不是 JSON：{exc}")
        return None


def _group(adapted) -> dict:
    cats, order = {}, []
    for recipe, hint in adapted:
        key = (hint or {}).get("key") or recipe.get("categoryKey") or DEFAULT_CATEGORY["key"]
        key = _slug(key, "cat")
        if key not in cats:
            cats[key] = {"key": key,
                         "name": (hint or {}).get("name") or DEFAULT_CATEGORY["name"],
                         "recipes": []}
            order.append(key)
        recipe["categoryKey"] = key
        cats[key]["recipes"].append(recipe)
    return {"categories": [cats[k] for k in order]}


def _merge(base_catalog, incoming: dict):
    """按 recipeKey 覆盖同名，其余追加；分类跟着走"""
    base = {"categories": []}
    for cat in _list(_dict(base_catalog).get("categories")):
        if isinstance(cat, dict):
            base["categories"].append({"key": cat.get("key"), "name": cat.get("name"),
                                       "recipes": list(cat.get("recipes") or [])})
    holder = {}
    for cat in base["categories"]:
        for recipe in cat["recipes"]:
            holder[recipe.get("recipeKey")] = cat

    added, replaced = [], []
    for cat in incoming.get("categories") or []:
        for recipe in cat.get("recipes") or []:
            key = recipe.get("recipeKey")
            where = holder.get(key)
            if where is not None:
                recipes = where["recipes"]
                for index, old in enumerate(recipes):
                    if old.get("recipeKey") == key:
                        recipes[index] = recipe
                        break
                replaced.append({"key": key, "name": recipe.get("name"),
                                 "category": where.get("name")})
                continue
            target = next((c for c in base["categories"] if c.get("key") == cat.get("key")), None)
            if target is None:
                target = {"key": cat.get("key"), "name": cat.get("name"), "recipes": []}
                base["categories"].append(target)
            target["recipes"].append(recipe)
            holder[key] = target
            added.append({"key": key, "name": recipe.get("name"), "category": target.get("name")})
    return base, added, replaced


# --------------------------------------------------------------- 对外接口

def _prepare(payload) -> tuple[dict, dict]:
    report = {"source": "", "format": "unknown", "mode": "preview",
              "categories": 0, "recipes": 0,
              "added": [], "replaced": [], "skipped": [], "warnings": []}
    ctx = {"warnings": report["warnings"], "skipped": report["skipped"],
           "used": set(), "keep_price": bool(_dict(payload).get("keepPrice"))}

    raw, report["source"] = _read_source(payload, report["warnings"])
    items, fmt, notes = _flatten(raw)
    report["format"] = fmt
    report["warnings"].extend(notes)
    if not items:
        report["warnings"].append("没有解析出任何模板")
        return {"categories": []}, report

    adapted = []
    for item, hint in items:
        if not _looks_like_template(item):
            name = _str(_first(item, "name", "title", "id", "recipeKey")).strip() \
                if isinstance(item, dict) else ""
            report["skipped"].append({"name": name or "（无名字）",
                                      "reason": "不是可识别的模板结构"})
            continue
        recipe, cat = _adapt_recipe(item, hint, ctx)
        if recipe:
            adapted.append((recipe, cat))
    return _group(adapted), report


def preview_payload(payload) -> dict:
    """只适配不落库：导入前先看会被裁成什么样"""
    catalog, report = _prepare(payload)
    catalog = mediagen.normalize(catalog)
    report["categories"] = len(catalog["categories"])
    report["recipes"] = sum(len(c["recipes"]) for c in catalog["categories"])
    return {"catalog": catalog, "report": report}


def import_payload(payload, mode: str = "merge") -> dict:
    """适配 + 落库。mode=merge 按 recipeKey 覆盖，replace 整包替换"""
    catalog, report = _prepare(payload)
    incoming = mediagen.normalize(catalog)
    if not incoming["categories"]:
        report["saved"] = False
        report["warnings"].append("没有可导入的模板，目录保持不变")
        return {"catalog": mediagen.get_catalog(seed=False), "report": report, "saved": False}

    if str(mode).lower() == "replace":
        final = incoming
        report["added"] = [{"key": r["recipeKey"], "name": r["name"], "category": c["name"]}
                           for c in final["categories"] for r in c["recipes"]]
        report["replaced"] = []
        report["mode"] = "replace"
    else:
        merged, added, replaced = _merge(mediagen.get_catalog(seed=False), incoming)
        final = mediagen.normalize(merged)
        report["added"], report["replaced"] = added, replaced
        report["mode"] = "merge"

    saved = mediagen.save_catalog(final)
    report["saved"] = True
    report["categories"] = len(saved["categories"])
    report["recipes"] = sum(len(c["recipes"]) for c in saved["categories"])
    return {"catalog": saved, "report": report, "saved": True}


def sample() -> dict:
    """控制台「示例模板」按钮用：一份最小可用的通用模板"""
    return {
        "schema": "missai.template/v1",
        "id": "portrait-studio",
        "title": "证件照生成",
        "category": {"key": "portrait", "name": "人像写真"},
        "description": "白底/蓝底证件照，按主体与底色生成",
        "author": "示例作者",
        "version": 1,
        "output": {"mediaType": "image", "count": 1},
        "model": {"name": "gpt-image-1"},
        "params": {"size": "1024x1536",
                   "aspectRatios": {"3:4": "1024x1536", "1:1": "1024x1024"}},
        "prompt": {
            "template": "{subject}，{background}背景，{style}，正面免冠，高清细节",
            "negative": "低质量, 模糊, 变形, 多余手指",
        },
        "inputs": [
            {"key": "subject", "label": "人物描述", "type": "textarea", "required": True,
             "placeholder": "如：短发的亚洲女性，浅色衬衫"},
            {"key": "background", "label": "背景色", "type": "select", "default": "white",
             "options": [{"value": "white", "label": "白色"},
                         {"value": "blue", "label": "蓝色"},
                         {"value": "gray", "label": "灰色"}]},
            {"key": "style", "label": "风格", "type": "select", "default": "photo",
             "options": [{"value": "photo", "label": "写实摄影"},
                         {"value": "studio", "label": "影棚布光"}]},
        ],
    }
