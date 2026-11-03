"""通用数值规则匹配器：把当前数值翻译成该档位的情绪 / 行为约束。

规则来自数据库（平台数值规则 JSON），本模块只做纯计算，不碰 DB。
支持的范围写法：
  "0 ~ 9"        → (0, 9)
  "-100 ~ -100"  → (-100, -100)
  "5"            → (5, 5)        单值区间
全角「～」、全角负号「－」、多余空格都会自动归一；无法解析的规则直接跳过。
"""
from __future__ import annotations

from typing import Dict, List, Optional


class EmotionMapper:
    def __init__(self, db_rules_list, stat_meta: Optional[Dict] = None):
        """接收数据库规则，兼容两种入参：

        · {属性名: [rule, ...]}（db.list_stat_rules() 的返回）
        · [{"stat": 属性名, "rules": [rule, ...], "initial_value": ..., ...}, ...]

        `stat_meta` 是 {属性名: {initial_value, min_value, max_value}}，
        给第一种入参补上初始值 / 上下限（db.list_stat_rule_meta() 的返回）。
        """
        items = db_rules_list.items() if isinstance(db_rules_list, dict) \
            else db_rules_list
        self.rules_map: Dict[str, List[dict]] = {}
        params: Dict[str, dict] = {}
        for item in items or []:
            if isinstance(item, dict):
                stat = str(item.get("stat") or "").strip()
                rules = item.get("rules")
                if stat:
                    params[stat] = item
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                stat, rules = item
                stat = str(stat or "").strip()
            else:
                continue
            if stat and isinstance(rules, list):
                self.rules_map[stat] = rules
        for stat, cfg in (stat_meta or {}).items():
            name = str(stat or "").strip()
            if name and isinstance(cfg, dict):
                params.setdefault(name, {}).update(cfg)

        # 初始值 / 上下限：建会话时写库，后处理时截断
        self.initial_stats: Dict[str, int] = {}
        self.bounds_map: Dict[str, dict] = {}
        for stat, cfg in params.items():
            iv = cfg.get("initial_value", 0)
            lo = cfg.get("min_value", -9999)
            hi = cfg.get("max_value", 9999)
            try:
                self.initial_stats[stat] = int(iv)
            except (TypeError, ValueError):
                self.initial_stats[stat] = 0
            try:
                lo, hi = int(lo), int(hi)
            except (TypeError, ValueError):
                lo, hi = -9999, 9999
            if lo > hi:
                lo, hi = hi, lo
            self.bounds_map[stat] = {"min": lo, "max": hi}

    def get_initial_stats(self) -> Dict[str, int]:
        """该角色所有状态的初始字典，用于新建会话时写库。"""
        return self.initial_stats.copy()

    def clamp_stats(self, stats: Dict) -> Dict:
        """按属性上下限截断；不在规则表里的属性原样保留。"""
        clamped = dict(stats or {})
        for name, value in list(clamped.items()):
            bounds = self.bounds_map.get(str(name))
            if not bounds:
                continue
            try:
                num = int(float(value))
            except (TypeError, ValueError):
                continue
            clamped[name] = max(bounds["min"], min(bounds["max"], num))
        return clamped

    @staticmethod
    def parse_range(range_str) -> Optional[tuple[float, float]]:
        """把区间字符串安全解析为数字区间；失败返回 None。"""
        text = str(range_str or "").strip()
        for ch in ("～", "—", "–", "－"):
            text = text.replace(ch, "~" if ch in ("～", "—", "–") else "-")
        text = text.replace(" ", "")
        if not text:
            return None
        if "~" not in text:
            try:
                v = float(text)
            except ValueError:
                return None
            return (v, v)
        parts = text.split("~")
        if len(parts) != 2:
            return None
        try:
            lo, hi = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        return (lo, hi) if lo <= hi else (hi, lo)   # 上下限写反也按区间处理

    def match(self, stat_name: str, value) -> Optional[dict]:
        """返回命中档位；属性无规则或数值非数字时返回 None。"""
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return self._match_range(num, self.rules_map.get(str(stat_name)))

    def _match_range(self, value: float, rules) -> Optional[dict]:
        for rule in rules or []:
            if not isinstance(rule, dict):
                continue
            bounds = self.parse_range(rule.get("range"))
            if bounds and bounds[0] <= value <= bounds[1]:
                return rule
        return None

    def get_state_prompt(self, current_stats) -> str:
        """{属性: 数值} → Prompt 段落；没有规则的属性只输出数值。"""
        parts: List[str] = []
        for stat_name, value in (current_stats or {}).items():
            name = str(stat_name)
            rule = self.match(name, value)
            if rule:
                parts.append(
                    f"【{name}：{value}】\n"
                    f"- 情绪状态：{rule.get('emotion') or '未知'}\n"
                    f"- 行为约束：{rule.get('behavior') or '无'}"
                )
            else:
                parts.append(f"【{name}：{value}】")
        return "\n\n".join(parts)
