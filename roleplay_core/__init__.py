"""roleplay_core：角色扮演分层核心。

目前包含数据模型层；后续按层拆出 prompt / 生成 / 输出处理等模块。
"""
from .models import ChatContext, Message

__all__ = ["ChatContext", "Message"]
