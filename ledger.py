"""账本核心逻辑：成员、分摊、净额、最小转账结算。

纯函数实现，不依赖 AstrBot，方便单元测试与复用。
金额统一以「分」为单位的整数存储，避免浮点误差（0.1 + 0.2 != 0.3 那类问题）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime


class LedgerError(Exception):
    """账本操作错误，消息内容可直接展示给用户。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(name: str) -> str:
    """清理昵称：合并空白、去掉控制字符、限制长度。"""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:16]


def fmt_yuan(cents: int) -> str:
    """分 -> 元字符串，固定两位小数（6800 -> '68.00'，-1350 -> '-13.50'）。"""
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    yuan, fen = divmod(abs(cents), 100)
    return f"{sign}{yuan}.{fen:02d}"


def split_amount(total: int, parts: int) -> list[int]:
    """把 total 分尽量平均分成 parts 份。

    余数从前往后各加 1 分，保证 sum(结果) == total，一分不差。
    例如 6800 分 / 3 人 -> [2267, 2267, 2266]。
    """
    total = int(total)
    if parts <= 0:
        raise LedgerError("分摊人数必须大于 0")
    if total < 0:
        raise LedgerError("金额不能为负数")
    share, rem = divmod(total, parts)
    return [share + (1 if i < rem else 0) for i in range(parts)]


# ---------------------------------------------------------------- 账本结构

def new_group() -> dict:
    """一个会话（群/私聊）的初始账本。"""
    return {"members": {}, "bills": [], "archive": [], "created_at": _now()}


def ensure_member(group: dict, uid: str, name: str) -> dict:
    """加入成员（已存在则更新昵称），返回该成员记录。"""
    uid = str(uid)
    members = group.setdefault("members", {})
    display = _safe_name(name) or f"用户{uid[-4:]}"

    if uid in members:
        old = members[uid].get("name")
        # 昵称变化时同步，但若和别人撞名则保留原名，避免账目归属错乱
        if display != old and display not in {
            m.get("name") for k, m in members.items() if k != uid
        }:
            members[uid]["name"] = display
        return members[uid]

    used = {m.get("name") for m in members.values()}
    final, n = display, 2
    while final in used:
        final = f"{display}#{n}"
        n += 1
    members[uid] = {"name": final, "joined_at": _now()}
    return members[uid]


def name_of(group: dict, uid: str) -> str:
    uid = str(uid)
    member = (group.get("members") or {}).get(uid)
    if member and member.get("name"):
        return member["name"]
    return f"用户{uid[-4:]}" if uid.isdigit() else str(uid)


def add_bill(
    group: dict,
    *,
    payer: str,
    amount: int,
    desc: str,
    participants: list[str],
    created_by: str,
) -> dict:
    """记一笔：payer 先垫付 amount 分，由 participants 平摊。"""
    amount = int(amount)
    if amount <= 0:
        raise LedgerError("金额必须大于 0")
    if amount > 100_000_00:
        raise LedgerError("单笔金额超过 10 万元，请确认是否输入有误")

    payer = str(payer)
    # 去重并保持顺序
    parts = [str(p) for p in dict.fromkeys(participants)]
    if not parts:
        raise LedgerError("至少要有一个分摊人")

    bill = {
        "id": uuid.uuid4().hex[:8],
        "payer": payer,
        "amount": amount,
        "desc": (desc or "未命名支出").strip()[:40] or "未命名支出",
        "participants": parts,
        "created_by": str(created_by),
        "created_at": _now(),
    }
    group.setdefault("bills", []).append(bill)
    return bill


def remove_bill(group: dict, bill_id: str) -> dict | None:
    bills = group.get("bills") or []
    for i, bill in enumerate(bills):
        if bill.get("id") == bill_id:
            return bills.pop(i)
    return None


# ---------------------------------------------------------------- 结算

def shares_of(bill: dict) -> dict[str, int]:
    """一笔账单里每个参与人的分摊额（分）。"""
    parts = bill.get("participants") or []
    if not parts:
        return {}
    amounts = split_amount(int(bill.get("amount") or 0), len(parts))
    return dict(zip([str(p) for p in parts], amounts))


def compute_net(group: dict) -> dict[str, int]:
    """每人净额（分）：正数 = 别人该给我，负数 = 我该给别人。

    所有净额之和恒为 0（每笔账单 payer +amount，参与者合计 -amount）。
    """
    net: dict[str, int] = {str(uid): 0 for uid in (group.get("members") or {})}
    for bill in group.get("bills") or []:
        amount = int(bill.get("amount") or 0)
        payer = str(bill.get("payer"))
        net[payer] = net.get(payer, 0) + amount
        for uid, share in shares_of(bill).items():
            net[uid] = net.get(uid, 0) - share
    return net


def make_transfers(net: dict[str, int]) -> list[tuple[str, str, int]]:
    """把净额转成转账方案：[(付款人, 收款人, 金额分)]。

    贪心策略：每次让欠得最多的人还给收得最多的人，能大幅减少转账笔数
    （严格最少笔数是 NP-hard，贪心在实践中已经足够好）。
    """
    debtors = [[str(u), -int(v)] for u, v in net.items() if int(v) < 0]
    creditors = [[str(u), int(v)] for u, v in net.items() if int(v) > 0]
    if not debtors or not creditors:
        return []

    debtors.sort(key=lambda x: (-x[1], x[0]))
    creditors.sort(key=lambda x: (-x[1], x[0]))

    transfers: list[tuple[str, str, int]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d_uid, d_amt = debtors[i]
        c_uid, c_amt = creditors[j]
        pay = min(d_amt, c_amt)
        if pay > 0:
            transfers.append((d_uid, c_uid, pay))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers


def total_spent(bills: list[dict]) -> int:
    return sum(int(b.get("amount") or 0) for b in bills or [])


def settle_up(group: dict) -> dict:
    """清账：当前未结账单整体归档，账本归零。"""
    bills = group.get("bills") or []
    if not bills:
        raise LedgerError("当前没有未结算的账单")

    transfers = make_transfers(compute_net(group))
    record = {
        "at": _now(),
        "count": len(bills),
        "total": total_spent(bills),
        "transfers": [
            {"from": f, "to": t, "amount": a} for f, t, a in transfers
        ],
        "bills": bills,
    }
    group.setdefault("archive", []).append(record)
    group["bills"] = []
    return record
