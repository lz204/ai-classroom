"""性格互补分组：研讨环节把学生按互补性格配对后分成若干小组。

契约（graph.grouping.build_groups）：
- 返回 [{"group_id": "group_1", "name": "第1组", "angle": "概念辨析",
         "member_ids": [...], "members": [姓名...]}]
- 配对规则：expert 与 introvert 互补、active 与 follower 互补；
- 多出的学生（未配成对的/超出的组）并入最后一组；
- 讨论角度默认 ["概念辨析", "例题应用"]，组数更多时用 "拓展探究N"。
"""
from config import STUDENT_GROUP_COUNT

# 互补配对规则：学霸型带内向型、活跃型带跟随型（性格互补，避免同质化扎堆）
_COMPLEMENTARY_PAIRS = (("expert", "introvert"), ("active", "follower"))

# 默认讨论角度；超过默认角度数量的组使用 "拓展探究N"
DEFAULT_ANGLES = ["概念辨析", "例题应用"]
_EXTRA_ANGLE_PREFIX = "拓展探究"


def _get(obj, key: str, default=None):
    """同时兼容 StudentAgent 对象与 dict 两种学生描述方式。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _pair_students(students: list) -> tuple[list, list]:
    """按互补规则配对，返回 (双人组列表, 未配对的单人列表)。

    配对顺序保持稳定：先 expert+introvert，再 active+follower；
    未配对的学生按原始顺序返回，供「并入最后一组」使用。
    """
    # 按性格分桶（保持原始学生顺序）
    buckets: dict[str, list] = {}
    for s in students:
        buckets.setdefault(_get(s, "personality", "follower"), []).append(s)

    pairs: list[list] = []
    for primary, complement in _COMPLEMENTARY_PAIRS:
        left = list(buckets.get(primary, []))
        right = list(buckets.get(complement, []))
        while left and right:
            pairs.append([left.pop(0), right.pop(0)])
        # 回写剩余部分，供最后收集单人身
        buckets[primary] = left
        buckets[complement] = right

    paired_ids = {id(m) for pair in pairs for m in pair}
    leftovers = [s for s in students if id(s) not in paired_ids]
    return pairs, leftovers


def _split_until(units: list[list], target_count: int) -> list[list]:
    """组数不足时拆开最后一对（双人组拆成两个单人组），直到组数达到目标。"""
    while len(units) < target_count:
        split_done = False
        for idx in range(len(units) - 1, -1, -1):
            if len(units[idx]) > 1:
                pair = units.pop(idx)
                units.extend([m] for m in pair)
                split_done = True
                break
        if not split_done:
            break
    return units


def build_groups(students: list, group_count: int = STUDENT_GROUP_COUNT) -> list[dict]:
    """把学生分成性格互补的研讨小组。

    :param students: StudentAgent 列表（或含 personality/student_id/name 的 dict）
    :param group_count: 目标组数（默认取 config.STUDENT_GROUP_COUNT）
    :return: 分组列表，结构见模块文档
    """
    students = list(students or [])
    if not students:
        return []

    # 组数不能超过学生数（避免出现空组）
    try:
        target = int(group_count or 1)
    except (TypeError, ValueError):
        target = 1
    count = max(1, min(target, len(students)))

    pairs, leftovers = _pair_students(students)
    # 以「组单元」为单位分配：互补对是 2 人单元，未配对的学生是 1 人单元
    units: list[list] = [list(pair) for pair in pairs] + [[s] for s in leftovers]
    units = _split_until(units, count)

    if len(units) > count:
        # 多出的学生/互补对全部并入最后一组（契约要求）
        merged = [m for unit in units[count - 1:] for m in unit]
        group_members = units[: count - 1] + [merged]
    else:
        group_members = units

    # 学生对象没有 student_id 时用其在班级中的序号兜底（保证 id 唯一）
    index_by_obj = {id(s): i + 1 for i, s in enumerate(students)}
    groups: list[dict] = []
    for idx, members in enumerate(group_members):
        if idx < len(DEFAULT_ANGLES):
            angle = DEFAULT_ANGLES[idx]
        else:
            # 组数更多时（第3组起）使用 "拓展探究N"（N 为组序号）
            angle = f"{_EXTRA_ANGLE_PREFIX}{idx + 1}"
        member_ids = [
            _get(m, "student_id") or f"student_{index_by_obj.get(id(m), 0)}" for m in members
        ]
        groups.append(
            {
                "group_id": f"group_{idx + 1}",
                "name": f"第{idx + 1}组",
                "angle": angle,
                # member_ids 与成员一一对应；members 为姓名列表（前端/提示词展示用）
                "member_ids": member_ids,
                "members": [_get(m, "name", "") for m in members],
            }
        )
    return groups
