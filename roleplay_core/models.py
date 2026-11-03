"""roleplay_core 数据模型层。

用强类型结构替代到处传裸 dict：
- Message：一条对话消息（content 是喂模型的纯文本；display_text/status/options 是展示态）
- ChatContext：一次生成所需的完整上下文（角色、历史、用户输入、AICG 开关）
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    """一条对话消息。"""

    model_config = ConfigDict(extra="ignore")

    role: Role
    content: str = ""                  # 喂模型的纯文本（已清洗，不含 UI 标签）
    id: int | str | None = None        # 库里 chat_messages.id；本地草稿可为 None
    created_at: str = ""
    display_text: str | None = None    # 前端展示态：状态栏/选项已剥离，保留 <say>/<image>
    status: list[dict[str, str]] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)

    def to_llm(self) -> dict[str, str]:
        """只保留模型需要的字段。"""
        return {"role": self.role, "content": self.content}


class ChatContext(BaseModel):
    """一次生成所需的上下文。

    character_data 约定键：
      character   角色整包（db.work_as_character 结果）
      promptData  提示词配置（systemPrompt / worldViewPrompt / postPrompt ...）
      cg          当前可用 CG 库（含 weights）；AICG 关闭时为 None
      name        角色名
      hasPayload  是否拿到角色整包（镜像角色可能拿不到）
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    character_data: dict[str, Any] = Field(default_factory=dict)
    history: list[Message] = Field(default_factory=list)
    user_input: str = ""
    enable_aicg: bool = True
    # 最近一次落库的状态栏（values 为 {属性名: 数值}，items 为展示用条目）
    current_status: dict[str, Any] = Field(default_factory=dict)
    # 平台数值规则（{属性名: [{range, emotion, behavior}]}），由服务层查库后注入
    stat_rules: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # 平台规则的初始值 / 上下限：{属性名: {initial_value, min_value, max_value}}
    stat_meta: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 角色卡声明的属性 schema：{属性名: {min, max, default}}
    stats_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def character(self) -> dict[str, Any]:
        ch = self.character_data.get("character")
        return ch if isinstance(ch, dict) else {}

    @property
    def prompt_data(self) -> dict[str, Any]:
        pd = self.character_data.get("promptData")
        return pd if isinstance(pd, dict) else {}

    @property
    def cg_data(self) -> dict[str, Any] | None:
        cg = self.character_data.get("cg")
        return cg if isinstance(cg, dict) else None

    @property
    def character_id(self) -> str:
        return str(self.character_data.get("id") or "")

    @property
    def character_name(self) -> str:
        return str(self.character_data.get("name") or "角色")

    @property
    def has_payload(self) -> bool:
        return bool(self.character_data.get("hasPayload"))

    def llm_history(self) -> list[dict[str, str]]:
        return [m.to_llm() for m in self.history]
