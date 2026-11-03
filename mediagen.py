#!/usr/bin/env python3
"""
mediagen.py — AI 绘图工作室（/ai-creator）的模板配方 + 本地生成

模板来源：data/json/ 下的内置模板文件（每类一份，默认 ai-draw-*.json；
          往里加新文件即成新内置模板，重启或「恢复内置」自动收录）
          也可用 POST /local/api/templates 导入任意 JSON 覆盖（重启用 /reset）

前端是源站打包代码，写不进去，只能喂符合它校验的数据：
  GET  /api/gva/aiMedia/creator/catalog?lang=zh            → {code:0,data:{categories:[…]}}
  POST /api/gva/aiMedia/creator/recipes/<key>/generations   → {code:0,data:{generationId,…}}
  GET  /api/gva/aiMedia/generations/<id>                    → 同上（assetStatus 轮询）

配方过不了前端 normalizeCatalog 就会被静默丢掉（页面显示“暂无模板”），
所以这里按前端规则先做一遍同样的裁剪：recipeKey/name 必须合法、
outputMediaType ∈ {image,video}、submitProtocol ∈ {aiMediaRecipe,aiImage}、
price.points ≥ 0、fields[].type ∈ {text,textarea,select}（select 必须有 options）。

recipe 里的 local 段只有后端用（前端 normalize 会把未知键丢掉），
用来配提示词前后缀、负向词、尺寸映射、字段值到提示词的映射。
"""

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path

import db
import imagegen
import imagedb

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "data" / "json"   # 内置模板目录：这里的 *.json 都是内置模板
IMAGE_DIR = ROOT / "html" / "image"
CATALOG_KEY = "aiMedia/creator/catalog"

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BAD_KEYS = {"__proto__", "prototype", "constructor"}
FIELD_TYPES = ("text", "textarea", "select")
MEDIA_TYPES = ("image", "video")
PROTOCOLS = ("aiMediaRecipe", "aiImage")
MAX_CATEGORIES = 20
MAX_RECIPES = 100

# 通用模板里的 {字段key} 占位符（外部模板常用写法）
PLACEHOLDER_RE = re.compile(r"\{\s*([A-Za-z0-9._:-]+)\s*\}")


# --------------------------------------------------------------- 模板规整

def _key_ok(value) -> bool:
    text = str(value or "").strip()
    return bool(KEY_RE.match(text)) and text not in BAD_KEYS


def _text(value, default="") -> str:
    return value if isinstance(value, str) else default


def _clean_field(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("key")).strip()
    label = _text(raw.get("label")).strip()
    kind = _text(raw.get("type"))
    if not _key_ok(key) or not label or kind not in FIELD_TYPES:
        return None
    options = []
    seen = set()
    for opt in raw.get("options") or []:
        if not isinstance(opt, dict):
            continue
        value = _text(opt.get("value")).strip()
        text = _text(opt.get("label")).strip()
        if not value or not text or value in seen:
            continue
        seen.add(value)
        options.append({"value": value, "label": text})
    default = _text(raw.get("defaultValue"))
    if kind == "select" and (not options or (default and default not in seen)):
        return None                     # 前端会整条丢掉，这里提前拦下
    field = {"key": key, "type": kind, "label": label, "required": bool(raw.get("required"))}
    if _text(raw.get("placeholder")).strip():
        field["placeholder"] = _text(raw.get("placeholder")).strip()
    if default:
        field["defaultValue"] = default
    if options:
        field["options"] = options
    return field


def _clean_slot(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = _text(raw.get("key")).strip()
    label = _text(raw.get("label")).strip()
    media = _text(raw.get("mediaType"))
    if not _key_ok(key) or not label or media not in MEDIA_TYPES:
        return None
    slot = {"key": key, "label": label, "mediaType": media,
            "required": bool(raw.get("required"))}
    if _text(raw.get("fieldKey")).strip():
        slot["fieldKey"] = _text(raw.get("fieldKey")).strip()
    return slot


def _clean_schema(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    fields = [_clean_field(f) for f in (raw.get("fields") or [])[:40]]
    slots = [_clean_slot(s) for s in (raw.get("mediaSlots") or [])[:8]]
    if any(f is None for f in fields) or any(s is None for s in slots):
        return None
    keys = [f["key"] for f in fields] + [s["key"] for s in slots]
    if len(set(keys)) != len(keys):
        return None
    # 前端只允许一个“主素材槽”（没有 fieldKey 的那个）
    if sum(1 for s in slots if not s.get("fieldKey")) > 1:
        return None
    return {"fields": fields, "mediaSlots": slots}


def _clean_recipe(raw, category_key: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    recipe_key = _text(raw.get("recipeKey")).strip()
    name = _text(raw.get("name")).strip()
    media_type = _text(raw.get("outputMediaType"))
    protocol = _text(raw.get("submitProtocol"))
    price = raw.get("price") if isinstance(raw.get("price"), dict) else {}
    schema = _clean_schema(raw.get("schema"))
    if not _key_ok(recipe_key) or not name or not schema:
        return None
    if media_type not in MEDIA_TYPES or protocol not in PROTOCOLS:
        return None
    try:
        points = int(price.get("points", -1))
    except (TypeError, ValueError):
        return None
    if points < 0:
        return None
    recipe = dict(raw)                  # 保留 local 等后端自用字段
    recipe.update({
        "recipeKey": recipe_key,
        "version": int(raw.get("version") or 0),
        "name": name,
        "description": _text(raw.get("description")),
        "coverUrl": _text(raw.get("coverUrl")),
        "outputMediaType": media_type,
        "submitProtocol": protocol,
        "categoryKey": _text(raw.get("categoryKey")) or category_key,
        "price": {"points": points,
                  "pricingMode": _text(price.get("pricingMode"), "fixed"),
                  "isEstimate": bool(price.get("isEstimate"))},
        "schema": schema,
        "sortOrder": int(raw.get("sortOrder") or 0),
    })
    return recipe


def normalize(raw) -> dict:
    """按前端 normalizeCatalog 的规则裁剪，保证喂过去不会被丢"""
    categories = []
    seen_cat = set()
    seen_recipe = set()
    for cat in (raw.get("categories") if isinstance(raw, dict) else None) or []:
        if len(categories) >= MAX_CATEGORIES:
            break
        if not isinstance(cat, dict):
            continue
        key = _text(cat.get("key")).strip()
        name = _text(cat.get("name")).strip()
        if not _key_ok(key) or not name or key in seen_cat:
            continue
        recipes = []
        for item in (cat.get("recipes") or [])[:MAX_RECIPES]:
            recipe = _clean_recipe(item, key)
            if not recipe or recipe["recipeKey"] in seen_recipe:
                continue
            seen_recipe.add(recipe["recipeKey"])
            recipes.append(recipe)
        if not recipes:
            continue
        recipes.sort(key=lambda r: r["sortOrder"])
        seen_cat.add(key)
        categories.append({"key": key, "name": name, "recipes": recipes})
    return {"categories": categories}


# --------------------------------------------------------------- 读写模板

def builtin_files() -> list:
    """内置模板文件：json/*.json（文件名即加载顺序，加文件就自动收录）"""
    try:
        return sorted(p for p in TEMPLATE_DIR.glob("*.json") if p.is_file())
    except OSError:
        return []


def load_builtin() -> dict:
    """把 json/ 下的内置模板文件合成一份目录

    同名分类合并；同一个 recipeKey 以后加载到的文件为准（文件名排序靠后的优先）。
    分类顺序看分类里的 sortOrder（没写就按文件出现顺序）。
    文件缺失 / 坏掉 → 跳过，不抛异常（页面照样能开）。
    """
    cats: dict = {}
    order: list = []
    sort_of: dict = {}
    seen: set = set()
    for index, path in enumerate(builtin_files()):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        for cat in raw.get("categories") or []:
            if not isinstance(cat, dict):
                continue
            key = str(cat.get("key") or cat.get("name") or "").strip()
            if not key:
                continue
            if key not in cats:
                cats[key] = {"key": key,
                             "name": str(cat.get("name") or key).strip() or key,
                             "recipes": []}
                order.append(key)
                try:
                    sort_of[key] = (float(cat.get("sortOrder")), index)
                except (TypeError, ValueError):
                    sort_of[key] = (float(index + 1), index)
            bucket = cats[key]["recipes"]
            for recipe in cat.get("recipes") or cat.get("templates") or []:
                if not isinstance(recipe, dict):
                    continue
                rkey = str(recipe.get("recipeKey") or "").strip()
                if not rkey:
                    continue
                if rkey in seen:
                    bucket[:] = [r for r in bucket if r.get("recipeKey") != rkey]
                seen.add(rkey)
                bucket.append(recipe)
    order.sort(key=lambda k: sort_of.get(k, (0.0, 0)))
    return {"categories": [cats[k] for k in order]}


def builtin_sources() -> list:
    """内置模板文件清单：恢复内置时告诉用户到底读了哪几个文件"""
    out = []
    for path in builtin_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        item = {"file": path.name, "path": rel, "categories": [],
                "recipes": 0, "error": ""}
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            item["error"] = f"读取失败：{exc}"
            out.append(item)
            continue
        for cat in (raw.get("categories") if isinstance(raw, dict) else None) or []:
            if not isinstance(cat, dict):
                continue
            item["categories"].append(str(cat.get("name") or cat.get("key") or ""))
            item["recipes"] += len([r for r in cat.get("recipes") or []
                                    if isinstance(r, dict)])
        out.append(item)
    return out


def reset_builtin() -> dict:
    """恢复内置：按 json/ 目录重建模板目录，返回 {catalog, files}"""
    catalog = save_catalog(load_builtin())
    return {"catalog": catalog, "files": builtin_sources()}


def save_catalog(raw) -> dict:
    catalog = normalize(raw)
    db.set_config(CATALOG_KEY, catalog)
    return catalog


def get_catalog(seed: bool = True) -> dict:
    """当前模板目录；空库时用 json/ 下的内置模板文件播种"""
    saved = db.get_config(CATALOG_KEY) or {}
    categories = saved.get("categories") if isinstance(saved, dict) else None
    if not categories and seed:
        return save_catalog(load_builtin())
    return {"categories": categories or []}


def find_recipe(recipe_key: str) -> dict | None:
    key = str(recipe_key or "").strip()
    if not key:
        return None
    for cat in get_catalog(seed=True)["categories"]:
        for recipe in cat.get("recipes") or []:
            if recipe.get("recipeKey") == key:
                return recipe
    return None


def delete_recipe(recipe_key: str) -> bool:
    """删掉一个配方（分类空了就一起删）；删完重新规整一次"""
    key = str(recipe_key or "").strip()
    if not key:
        return False
    catalog = get_catalog(seed=False)
    changed = False
    keep = []
    for cat in catalog.get("categories") or []:
        recipes = [r for r in cat.get("recipes") or [] if r.get("recipeKey") != key]
        if len(recipes) != len(cat.get("recipes") or []):
            changed = True
        if recipes:
            keep.append(dict(cat, recipes=recipes))
        elif cat.get("recipes"):
            changed = True             # 整类被删空
    if not changed:
        return False
    save_catalog({"categories": keep})
    return True


# --------------------------------------------------------------- 生成

def _local_conf(recipe: dict) -> dict:
    conf = recipe.get("local")
    return conf if isinstance(conf, dict) else {}


def _option_label(field: dict, value: str) -> str:
    for opt in field.get("options") or []:
        if str(opt.get("value")) == value:
            return str(opt.get("label") or value)
    return value


def build_prompt(recipe: dict, fields: dict) -> str:
    """字段值 → 提示词：前缀 + 各字段(主描述/选项映射) + 后缀"""
    conf = _local_conf(recipe)
    mapping = conf.get("fieldPrompt") if isinstance(conf.get("fieldPrompt"), dict) else {}

    # 通用模板：promptTemplate 里用 {字段key} 拼句子，没填的字段就地留空
    template = str(conf.get("promptTemplate") or "").strip()
    if template:
        by_key = {f["key"]: f for f in recipe["schema"]["fields"]}

        def _value(match) -> str:
            key = match.group(1)
            raw = str(fields.get(key) or "").strip()
            if not raw:
                return ""
            field = by_key.get(key) or {}
            if field.get("type") == "select":
                mapped = mapping.get(key) if isinstance(mapping.get(key), dict) else {}
                return str(mapped.get(raw) or _option_label(field, raw))
            return raw

        text = PLACEHOLDER_RE.sub(_value, template)
        text = PLACEHOLDER_RE.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s*[，,；;]\s*(?=[，,；;])", "", text)
        text = text.strip(" \t，,；;")
        prefix = str(conf.get("promptPrefix") or "")
        suffix = str(conf.get("promptSuffix") or "")
        return f"{prefix}{text}{suffix}".strip()

    pieces = []
    for field in recipe["schema"]["fields"]:
        key = field["key"]
        value = str(fields.get(key) or "").strip()
        if not value or key == "aspectRatio" or field.get("type") == "select":
            continue
        pieces.append(value)
    for field in recipe["schema"]["fields"]:
        key = field["key"]
        if field.get("type") != "select" or key == "aspectRatio" or key == "detail":
            continue
        value = str(fields.get(key) or "").strip()
        if not value:
            continue
        mapped = mapping.get(key) if isinstance(mapping.get(key), dict) else {}
        pieces.append(str(mapped.get(value) or _option_label(field, value)))
    body = "，".join([p for p in pieces if p])
    prefix = str(conf.get("promptPrefix") or "")
    suffix = str(conf.get("promptSuffix") or "")
    detail = conf.get("detailPrompt") if isinstance(conf.get("detailPrompt"), dict) else {}
    tail = str(detail.get(str(fields.get("detail") or "")) or "")
    return f"{prefix}{body}{tail}{suffix}"


def pick_size(recipe: dict, fields: dict) -> str:
    conf = _local_conf(recipe)
    ratio = str(fields.get("aspectRatio") or "").strip()
    table = conf.get("sizeByRatio") if isinstance(conf.get("sizeByRatio"), dict) else {}
    return str(table.get(ratio) or conf.get("size") or "1024x1024")


def pick_seed(fields: dict) -> int:
    try:
        return max(0, min(2147483647, int(str(fields.get("seed") or 0))))
    except (TypeError, ValueError):
        return 0


def validate_fields(recipe: dict, fields: dict) -> str:
    """返回错误文案；空串 = 通过（与前端 I() 的规则一致）"""
    for field in recipe["schema"]["fields"]:
        value = str(fields.get(field["key"]) or "").strip()
        if field.get("required") and not value:
            return f"请填写{field['label']}。"
        if field.get("type") == "select" and value:
            if not any(str(o.get("value")) == value for o in field.get("options") or []):
                return f"{field['label']}选项已失效，请重新选择。"
    for slot in recipe["schema"]["mediaSlots"]:
        if slot.get("required"):
            return f"本地模式暂不支持上传{slot['label']}，请换用文生图模板。"
    return ""


def define_endpoint(recipe: dict) -> dict:
    """模板 → 本地绘图接入（与绘图广场同一套接入配置）"""
    conf = _local_conf(recipe)
    target = db.resolve_image_rate_id(conf.get("rateId"))
    if target:
        row, model = target
        if str(row.get("kind")) == "local" or str(row.get("base_url") or "").startswith("local://"):
            return {"local": True, "model": model, "provider_id": int(row["id"]),
                    "provider_name": str(row.get("name") or "")}
        base, key = db.image_credentials(row["id"]) or ("", "")
        return {"local": False, "base": base, "key": key,
                "model": str(conf.get("model") or model),
                "provider_id": int(row["id"]),
                "provider_name": str(row.get("name") or "")}

    endpoint = db.active_image_endpoint()
    if endpoint:
        base, key, model, _sizes = endpoint
        prov = db.one("SELECT id, name, kind, base_url FROM image_providers"
                      " WHERE enabled=1 ORDER BY is_default DESC, id ASC") or {}
        local = (str(prov.get("kind") or "") == "local"
                 or str(base or "").startswith("local://"))
        return {"local": local, "base": base, "key": key,
                "model": str(conf.get("model") or model),
                "provider_id": int(prov.get("id") or 0) or None,
                "provider_name": str(prov.get("name") or "")}

    return {"local": True, "model": str(conf.get("model") or "local-placeholder"),
            "provider_id": None, "provider_name": "本地占位绘图"}


def payload_of(row: dict) -> dict:
    """数据库行 → 前端 acceptGeneration 认的形状。

    不返 recordId：源站用它打开收藏/公开按钮，而这两个接口走线上图库，
    本地没有对应记录；生成图本来就已经进了 image_works（绘图广场/图库可见）。
    """
    data = {
        "generationId": row["id"],
        "modelKey": row.get("model") or "",
        "mode": row.get("recipe_key") or "",
        "mediaType": row.get("media_type") or "image",
        "status": row.get("status") or "submitted",
        "assetStatus": row.get("asset_status") or "pending",
        "assetUrl": row.get("asset_url") or "",
        "chargedCredits": int(row.get("charged_credits") or 0),
        "refunded": bool(row.get("refunded") or 0),
        "failureCode": row.get("failure_code") or "",
        "createdAt": row.get("created_at") or "",
    }
    if row.get("failure_msg"):
        data["failureMessage"] = row["failure_msg"]
    return data


def get_generation(gid: str, uid) -> dict | None:
    row = db.media_generation_get(gid)
    if not row:
        return None
    owner = row.get("user_id")
    if uid and owner not in (None, uid):
        return None                     # 别人的任务不返回
    return row


def create_generation(uid: int, author: str, recipe: dict, body: dict,
                      idempotency_key: str = "") -> tuple[dict | None, str, str]:
    """建任务 + 起后台线程。返回 (行, 错误码, 错误文案)"""
    idem = str(idempotency_key or "").strip()
    if idem:
        done = db.media_generation_by_key(uid, idem)
        if done:
            return done, "", ""         # 幂等：同一 key 直接回原任务

    version = int(body.get("recipeVersion") or 0)
    if version and int(recipe.get("version") or 0) and version != int(recipe["version"]):
        return None, "RECIPE_VERSION_STALE", "模板已更新，请刷新后重试。"

    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    fields = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    err = validate_fields(recipe, fields)
    if err:
        return None, "MEDIA_INVALID_REQUEST", err

    prompt = build_prompt(recipe, fields)
    if not str(prompt).strip():
        return None, "MEDIA_INVALID_REQUEST", "请先写画面描述。"

    conf = _local_conf(recipe)
    gid = uuid.uuid4().hex
    row = db.media_generation_create(
        uid, gid, recipe["recipeKey"], int(recipe.get("version") or 0),
        prompt, str(conf.get("negative") or ""), pick_size(recipe, fields),
        pick_seed(fields), idem, body)
    if not row:
        return None, "MEDIA_INTERNAL_ERROR", "生成任务创建失败。"

    threading.Thread(target=_run, args=(gid, recipe, author),
                     daemon=True, name=f"mediagen-{gid[:8]}").start()
    return row, "", ""


def _run(gid: str, recipe: dict, author: str) -> None:
    """后台生成：绘图接入 → 落盘 → 入图片库 + 绘图广场 → 回写任务状态"""
    row = db.media_generation_get(gid)
    if not row:
        return
    conf = _local_conf(recipe)
    try:
        endpoint = define_endpoint(recipe)
        db.media_generation_set(gid, status="running", model=endpoint.get("model") or "")
        n = max(1, min(4, int(conf.get("count") or 1)))
        if endpoint.get("local"):
            images = [imagegen.placeholder_png(row["prompt"], row["size"],
                                              int(row["seed"] or 0)) for _ in range(n)]
        else:
            images = imagegen.generate(endpoint.get("base") or "", endpoint.get("key") or "",
                                       endpoint.get("model") or "", row["prompt"],
                                       row["negative"] or "", row["size"] or "1024x1024",
                                       n, seed=int(row["seed"] or 0))
        if not images:
            raise imagegen.ImageGenError("上游没有返回可用的图片数据")

        data = images[0]
        sha = hashlib.sha256(data).hexdigest()[:8]
        name = f"ai_{int(time.time() * 1000)}_{sha}{imagegen.detect_ext(data)}"
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        target = IMAGE_DIR / name
        target.write_bytes(data)
        url_key = f"/image/{name}"
        registered = imagedb.register(url_key, target, "ai")
        work = db.add_image_work(
            row.get("user_id"), author or "本地用户", row["prompt"], row["negative"] or "",
            endpoint.get("provider_id"), endpoint.get("provider_name") or "",
            endpoint.get("model") or "", row["size"] or "", url_key,
            (registered or {}).get("id"), seed=int(row["seed"] or 0))
        db.media_generation_set(
            gid, status="succeeded", asset_status="available", asset_url=url_key,
            image_work_id=(work or {}).get("id"),
            charged_credits=0, failure_code="", failure_msg="")
        db.db_log("MEDIA_GENERATE", "media_generations",
                  json.dumps({"recipe": recipe.get("recipeKey"),
                              "model": endpoint.get("model"), "url": url_key},
                             ensure_ascii=False), row.get("user_id"))
    except imagegen.ImageGenError as exc:
        db.media_generation_set(gid, status="failed", asset_status="missing",
                                failure_code="MEDIA_GENERATION_FAILED",
                                failure_msg=str(exc)[:500])
        db.db_log("MEDIA_GENERATE_FAIL", "media_generations",
                  json.dumps({"recipe": recipe.get("recipeKey"),
                              "error": str(exc)[:300]}, ensure_ascii=False), row.get("user_id"))
    except Exception as exc:  # noqa: BLE001 —— 后台线程不能把服务带崩
        db.media_generation_set(gid, status="failed", asset_status="missing",
                                failure_code="MEDIA_INTERNAL_ERROR",
                                failure_msg=str(exc)[:500])
