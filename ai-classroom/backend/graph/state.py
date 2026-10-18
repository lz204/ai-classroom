"""LangGraph 共享状态定义：课堂全局状态与字段合并规则。"""
from typing import Annotated, TypedDict

import operator


def _merge_order_alerts(left: list, right: list) -> list:
    """秩序告警合并规则：追加新告警；manager 显式返回空列表([])时视为清空。

    LangGraph 只在节点返回了该字段时才调用 reducer：
    - 学生节点产生告警时返回告警列表 -> 追加
    - 学生节点无告警时不返回该字段 -> 不触发
    - manager 处理完告警返回 [] -> 清空，避免反复跳转 manager 死循环
    """
    if right == []:
        return []
    return (left or []) + list(right)


class ClassroomState(TypedDict):
    """课堂共享状态：messages 用 operator.add 增量合并，避免覆盖历史。"""

    # 对话历史（追加式）
    messages: Annotated[list, operator.add]
    # 学生节点本次新增、待仲裁器排序的消息（覆盖式）
    new_messages: list
    # 当前发言角色
    current_speaker: str
    # 课堂阶段（展示字段，路由不依赖它，由 teacher 按 round 重写）
    phase: str
    # 授课主题
    topic: str
    # 记忆上下文（学生群、发言队列、三层记忆等，节点间共享）
    memory: dict
    # 秩序告警（追加 + 可清空）
    order_alerts: Annotated[list, _merge_order_alerts]
    # teacher 执行轮次（单调递增，路由的唯一依据）
    round: int
