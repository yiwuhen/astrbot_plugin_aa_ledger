"""AstrBot 插件：群内 AA 记账。

一句话记一笔（「aa 68 午饭」或直接说「午饭 68 我先付了」），
月底一条命令出「谁该给谁多少钱」的最小转账方案。

核心设计
  - 金额全程用「分」做整数运算，分摊余数逐分分配，保证一分不差
  - 账本按会话（群 / 私聊）隔离，互不干扰
  - 结算用贪心算法压缩转账笔数，5 个人吃饭通常 4 笔以内结清
  - 数据落在 data/plugin_data/astrbot_plugin_aa_ledger/，重装插件不丢
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

_PLUGIN_ID = "astrbot_plugin_aa_ledger"

# 极老版本 AstrBot 没有 filter.llm_tool，退化为空装饰器，避免整个插件加载失败
_llm_tool = getattr(filter, "llm_tool", None) or (lambda *a, **k: (lambda fn: fn))
_DATA_FILE = ""
_DATA_DIR = ""


def _load_sibling(module: str):
    """按文件路径加载同目录模块。

    不用 `import xx` 是为了避开 AstrBot 热重载时的 sys.modules 缓存污染
    （同名残留会导致加载到旧版本代码），模块名也带上插件前缀避免冲突。
    """
    name = f"{_PLUGIN_ID}.{module}"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{module}.py")
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ledger = _load_sibling("ledger")
parse = _load_sibling("parse")

LedgerError = ledger.LedgerError
fmt_yuan = ledger.fmt_yuan

# 子命令关键词
_SUB_HELP = {"帮助", "help", "?", "？", "说明"}
_SUB_LIST = {"明细", "列表", "log", "记录", "流水"}
_SUB_SETTLE = {"结算", "算账", "settle", "aa结算"}
_SUB_CLEAR = {"清账", "清零", "clear", "aa清账"}
_SUB_UNDO = {"撤销", "删", "撤回", "undo", "取消"}
_SUB_MEMBER = {"成员", "members", "谁"}
_SUB_JOIN = {"加入", "join"}
_SUB_LEAVE = {"退出", "leave", "退出账本"}
_SUB_ARCHIVE = {"归档", "历史", "history", "已结算"}

_ALL_SUBS = (
    _SUB_HELP | _SUB_LIST | _SUB_SETTLE | _SUB_CLEAR | _SUB_UNDO
    | _SUB_MEMBER | _SUB_JOIN | _SUB_LEAVE | _SUB_ARCHIVE
)

_HELP_TEXT = """📖 AA 记账 · 命令

【记账】
aa 68 午饭               记一笔（你垫付，全体成员平摊）
aa 68 午饭 @张三 @李四    只让这几个人分摊
aa 68 午饭 除@王五        全体成员里去掉王五
aa 六十八块 打车          中文金额也认

【查看】
aa                      账本概况 + 我的净额
aa 明细                 最近 10 笔账单

【结算】
aa 结算                 谁该给谁多少钱（附 @所有人）
aa 清账                 结算完成后把账本归零（会二次确认）
aa 归档                 历史上已结清的记录

【其他】
aa 成员 / aa 加入 / aa 退出
aa 撤销                 撤回最后一笔（限记账人或垫付人）"""


def _strip_cmd(text: str, *names: str) -> str:
    """剥掉命令词，返回参数部分。"""
    text = (text or "").strip()
    for name in names:
        if not name:
            continue
        if text.lower().startswith(name.lower()):
            rest = text[len(name):]
            if not rest or rest[0] in " \t":
                return rest.strip()
    return text


def _sender_id(event) -> str:
    for attr in ("get_sender_id", "get_user_id"):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                value = fn()
                if value not in (None, ""):
                    return str(value)
            except Exception:
                pass
    return "unknown"


def _sender_name(event) -> str:
    fn = getattr(event, "get_sender_name", None)
    if callable(fn):
        try:
            value = fn()
            if value:
                return str(value)
        except Exception:
            pass
    return ""


@register(
    _PLUGIN_ID,
    "WuHen",
    "群内 AA 记账：一句话记一笔，月底一键结算谁该给谁多少钱（最小转账方案）",
    "0.2.0",
)
class AaLedgerPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ------------------------------------------------------------ 生命周期

    async def initialize(self):
        global _DATA_FILE, _DATA_DIR
        _DATA_DIR = self._resolve_data_dir()
        _DATA_FILE = os.path.join(_DATA_DIR, "ledger.json")
        self._migrate_legacy()
        data = self._load()
        groups = len(data.get("groups") or {})
        bills = sum(len((g or {}).get("bills") or []) for g in (data.get("groups") or {}).values())
        logger.info(f"AA记账: 已加载 {groups} 个账本 / {bills} 笔未结账单（{_DATA_DIR}）")

    def _resolve_data_dir(self) -> str:
        try:
            path = StarTools.get_data_dir(_PLUGIN_ID)
            if path:
                path = str(path)
                os.makedirs(path, exist_ok=True)
                return path
        except Exception as exc:
            logger.warning(f"AA记账: 获取插件数据目录失败（{exc}），退回插件目录")
        fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(fallback, exist_ok=True)
        return fallback

    def _migrate_legacy(self):
        """旧版把数据写在插件目录里，这里自动搬到数据目录。"""
        legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.json")
        if not os.path.exists(legacy):
            return
        try:
            if os.path.exists(_DATA_FILE):
                os.remove(legacy)
                logger.info("AA记账: 新数据目录已有数据，已清理插件目录旧文件")
                return
            os.replace(legacy, _DATA_FILE)
            logger.info("AA记账: 已把旧版数据迁移到插件数据目录")
        except Exception as exc:
            logger.warning(f"AA记账: 旧数据迁移失败: {exc}")

    # ------------------------------------------------------------ 存储

    def _load(self) -> dict:
        if not _DATA_FILE or not os.path.exists(_DATA_FILE):
            return {"groups": {}}
        try:
            with open(_DATA_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("根节点不是对象")
            data.setdefault("groups", {})
            return data
        except Exception as exc:
            logger.error(f"AA记账: 读取 {_DATA_FILE} 失败: {exc}")
            return {"groups": {}}

    def _save(self, data: dict):
        if not _DATA_FILE:
            return
        tmp = f"{_DATA_FILE}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _DATA_FILE)
        except Exception as exc:
            logger.error(f"AA记账: 保存失败: {exc}")

    def _group(self, data: dict, key: str) -> dict:
        groups = data.setdefault("groups", {})
        if key not in groups or not isinstance(groups[key], dict):
            groups[key] = ledger.new_group()
        group = groups[key]
        group.setdefault("members", {})
        group.setdefault("bills", [])
        group.setdefault("archive", [])
        return group

    # ------------------------------------------------------------ 工具

    def _sym(self) -> str:
        return str(self.config.get("currency", "¥") or "¥")

    def _money(self, cents: int) -> str:
        return f"{self._sym()}{fmt_yuan(cents)}"

    @staticmethod
    def _extract_at(event) -> list[tuple[str, str]]:
        """取消息里的 @ 对象：[(uid, 昵称)]，取不到就返回空。"""
        out: list[tuple[str, str]] = []
        try:
            components = getattr(event.message_obj, "message", None) or []
        except Exception:
            return out
        for comp in components:
            qq = getattr(comp, "qq", None)
            if qq is None:
                continue
            qq = str(qq)
            if qq == "all" or not qq.isdigit():
                continue
            name = str(getattr(comp, "name", "") or "")
            if all(uid != qq for uid, _ in out):
                out.append((qq, name))
        return out

    async def _settle_result(self, event, text: str):
        """构造结算结果，可选带 @所有人。

        带 At 的消息改用 event.send() 主动发送并捕获异常：
        部分协议端（NapCat/Lagrange）对 At(qq="all") 处理不稳定，
        会返回 retcode=1200 timeout，若走 yield 管道会直接报错。
        发送失败时降级为纯文本，保证结算方案一定发得出去。
        """
        if not bool(self.config.get("at_all_on_settle", True)):
            return event.plain_result(text)
        try:
            comps = None
            for mod_path in ("astrbot.api.message_components", "astrbot.core.message.components"):
                try:
                    mod = importlib.import_module(mod_path)
                    comps = (getattr(mod, "At"), getattr(mod, "Plain"))
                    break
                except Exception:
                    continue
            if comps is None:
                raise ImportError("no components module")
            At, Plain = comps
            await event.send(event.chain_result([At(qq="all"), Plain(" " + text)]))
            return None  # 已发送，无需再 yield
        except Exception as e:
            logger.warning(f"带 @所有人 发送结算消息失败，降级为纯文本: {e}")
        return event.plain_result(text)

    # ------------------------------------------------------------ 记账主流程

    async def _do_record(self, event, key: str, raw: str):
        """解析并写入一笔账单，返回要回复的文本。"""
        data = self._load()
        group = self._group(data, key)
        uid = _sender_id(event)
        name = _sender_name(event)

        found = parse.extract_amount(raw)
        if not found:
            return f"没看到金额 🤔\n用法：aa 68 午饭（你垫付，全员平摊）\n发「aa 帮助」看全部命令"

        cents, frag = found
        old_members = dict(group.get("members") or {})
        ledger.ensure_member(group, uid, name)

        at_list = self._extract_at(event)
        for at_uid, at_name in at_list:
            if at_uid not in group["members"]:
                ledger.ensure_member(group, at_uid, at_name or f"用户{at_uid[-4:]}")

        mentioned = parse.find_member_ids(raw, group["members"])
        picked = list(dict.fromkeys(
            [u for u, _ in at_list if u in group["members"]] + mentioned
        ))
        exclude_mode = parse.has_exclude_word(raw) and bool(picked)

        members = list(group["members"])
        if exclude_mode:
            participants = [m for m in members if m not in picked]
        elif picked:
            participants = picked
        else:
            participants = members

        if not participants:
            return "分摊人不能为空——如果用了「除」，别把所有人都排除掉 🙃"

        drop_names = [
            (group["members"].get(m) or {}).get("name", "") for m in picked
        ] + [n for _, n in at_list if n]
        desc = parse.clean_desc(raw, frag, [n for n in drop_names if n]) or "未命名支出"

        try:
            bill = ledger.add_bill(
                group,
                payer=uid,
                amount=cents,
                desc=desc,
                participants=participants,
                created_by=uid,
            )
        except LedgerError as exc:
            return f"记账失败：{exc}"

        self._save(data)

        shares = ledger.shares_of(bill)
        each = sorted(set(shares.values()))
        if len(each) == 1:
            each_desc = f"每人 {self._money(each[0])}"
        else:
            each_desc = f"每人 {self._money(each[0])}~{self._money(each[-1])}"

        names = "、".join(ledger.name_of(group, p) for p in participants)
        bill_total = ledger.total_spent(group["bills"])

        lines = [
            "✅ 已记账",
            "",
            f"📝 {bill['desc']}  {self._money(cents)}",
            f"👤 垫付：{ledger.name_of(group, uid)}",
            f"👥 分摊：{len(participants)} 人（{each_desc}）",
        ]
        if len(names) <= 40:
            lines.append(f"   {names}")
        lines += [
            "",
            f"账本累计：{len(group['bills'])} 笔 / {self._money(bill_total)}",
            "「aa 结算」看方案 ·「aa 撤销」撤回这笔",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------ 命令：主入口

    @filter.command("aa", alias={"AA", "记账"})
    async def cmd_aa(self, event: AstrMessageEvent):
        """AA 记账主入口：aa [金额] [描述] | aa 明细 | aa 结算 | ..."""
        key = event.unified_msg_origin
        raw = _strip_cmd(event.message_str, "aa", "AA", "记账").strip()

        if not raw:
            yield event.plain_result(self._build_overview(key, _sender_id(event)))
            return

        head = raw.split(maxsplit=1)[0]
        word = head.lower()
        rest = raw[len(head):].strip()

        if word in _SUB_HELP:
            yield event.plain_result(_HELP_TEXT)
            return
        if word in _SUB_SETTLE:
            result = await self._settle_result(
                event, self._build_settle(key, _sender_id(event)),
            )
            if result is not None:
                yield result
            return
        if word in _SUB_LIST:
            yield event.plain_result(self._build_list(key))
            return
        if word in _SUB_CLEAR:
            yield event.plain_result(self._do_clear(key, confirmed=rest in ("确认", "确定", "yes", "y")))
            return
        if word in _SUB_UNDO:
            yield event.plain_result(self._do_undo(key, _sender_id(event)))
            return
        if word in _SUB_MEMBER:
            yield event.plain_result(self._build_members(key))
            return
        if word in _SUB_JOIN:
            yield event.plain_result(self._do_join(key, event))
            return
        if word in _SUB_LEAVE:
            yield event.plain_result(self._do_leave(key, event))
            return
        if word in _SUB_ARCHIVE:
            yield event.plain_result(self._build_archive(key))
            return

        yield event.plain_result(await self._do_record(event, key, raw))

    # ------------------------------------------------------------ 独立命令：结算

    @filter.command("结算", alias={"算账"})
    async def cmd_settle(self, event: AstrMessageEvent):
        """直接出结算方案"""
        result = await self._settle_result(
            event,
            self._build_settle(event.unified_msg_origin, _sender_id(event)),
        )
        if result is not None:
            yield result

    # ------------------------------------------------------------ 视图构建

    def _build_overview(self, key: str, uid: str = "") -> str:
        data = self._load()
        group = self._group(data, key)
        members = group["members"]
        bills = group["bills"]

        if not members:
            return (
                "📒 账本还是空的\n\n"
                "记第一笔：「aa 68 午饭」\n"
                "  默认你垫付、全员平摊，参与者随用随加\n"
                "加人：「aa 加入」或记账时 @对方\n\n"
                "发「aa 帮助」看全部命令"
            )

        if not bills:
            return (
                f"📒 账本 · {len(members)} 位成员\n\n"
                "暂无未结算账单，很干净 ✨\n"
                "记一笔：「aa 68 午饭」"
            )

        net = ledger.compute_net(group)
        transfers = ledger.make_transfers(net)
        total = ledger.total_spent(bills)

        lines = [
            f"📒 账本 · {len(members)} 人 · 未结 {len(bills)} 笔 · {self._money(total)}",
            "",
            "最近：",
        ]
        for bill in bills[::-1][:3]:
            lines.append(
                f"  · {bill['desc']} {self._money(bill['amount'])}"
                f"（{ledger.name_of(group, bill['payer'])}垫付，{len(bill['participants'])}人）"
            )
        lines += ["", f"结清需要 {len(transfers)} 笔转账", ""]

        my = net.get(uid)
        if my is None:
            lines.append("（你还没有账目往来）")
        elif my > 0:
            lines.append(f"我：待收 {self._money(my)}")
        elif my < 0:
            lines.append(f"我：待付 {self._money(-my)}")
        else:
            lines.append("我：已平")
        lines += ["", "「aa 结算」看方案 ·「aa 明细」看流水"]
        return "\n".join(lines)

    def _build_list(self, key: str) -> str:
        data = self._load()
        group = self._group(data, key)
        bills = group["bills"]
        if not bills:
            return "还没有未结算的账单。记一笔：「aa 68 午饭」"

        limit = int(self.config.get("max_bills_display", 10) or 10)
        shown = bills[::-1][:limit]
        lines = [f"📋 未结账单 {len(bills)} 笔（显示最近 {len(shown)} 笔）", ""]
        for idx, bill in enumerate(shown, start=1):
            stamp = (bill.get("created_at") or "")[5:16]
            parts = bill["participants"]
            shares = ledger.shares_of(bill)
            each = sorted(set(shares.values()))
            each_desc = (
                f"每人 {self._money(each[0])}" if len(each) == 1
                else f"每人 {self._money(each[0])}~{self._money(each[-1])}"
            )
            lines.append(
                f"{idx}. [{stamp}] {bill['desc']}  {self._money(bill['amount'])}"
            )
            lines.append(
                f"   {ledger.name_of(group, bill['payer'])}垫付 · "
                f"{len(parts)} 人 · {each_desc}"
            )
        if len(bills) > len(shown):
            lines.append(f"… 另有 {len(bills) - len(shown)} 笔更早的")
        lines += ["", "「aa 结算」出方案 ·「aa 撤销」撤回最后一笔"]
        return "\n".join(lines)

    def _build_settle(self, key: str, uid: str = "") -> str:
        data = self._load()
        group = self._group(data, key)
        bills = group["bills"]
        if not bills:
            return "当前没有未结算的账单，账目是平的 ✨"

        net = ledger.compute_net(group)
        transfers = ledger.make_transfers(net)
        total = ledger.total_spent(bills)

        if not transfers:
            return (
                f"✨ 账目已平\n\n{len(bills)} 笔账单合计 {self._money(total)}，"
                "但每个人收支正好抵消，谁也不欠谁。\n发「aa 清账」可把账本归零。"
            )

        lines = [
            f"💸 结算方案",
            f"{len(bills)} 笔未结 · 合计 {self._money(total)}",
            "",
        ]
        for i, (src, dst, amount) in enumerate(transfers, start=1):
            lines.append(
                f"{i}. {ledger.name_of(group, src)} → "
                f"{ledger.name_of(group, dst)}  {self._money(amount)}"
            )
        lines += [
            "",
            f"共 {len(transfers)} 笔转账即可全部结清",
            "转完发「aa 清账」把账本归零",
        ]

        # 个人视角
        my = net.get(uid, 0)
        if my > 0:
            lines.append(f"你（{ledger.name_of(group, uid)}）应收 {self._money(my)}")
        elif my < 0:
            lines.append(f"你（{ledger.name_of(group, uid)}）应付 {self._money(-my)}")
        return "\n".join(lines)

    def _build_members(self, key: str) -> str:
        data = self._load()
        group = self._group(data, key)
        members = group["members"]
        if not members:
            return "账本还没有成员。发「aa 加入」把自己加进来。"
        net = ledger.compute_net(group)
        lines = [f"👥 账本成员 {len(members)} 人", ""]
        for i, (uid, info) in enumerate(members.items(), start=1):
            value = net.get(uid, 0)
            if value > 0:
                tag = f"待收 {self._money(value)}"
            elif value < 0:
                tag = f"待付 {self._money(-value)}"
            else:
                tag = "已平"
            lines.append(f"{i}. {info.get('name') or uid}  ·  {tag}")
        return "\n".join(lines)

    def _build_archive(self, key: str) -> str:
        data = self._load()
        group = self._group(data, key)
        archive = group["archive"]
        if not archive:
            return "还没有已结清的记录。结完账发「aa 清账」就会归档到这里。"
        lines = ["🗂 已结算记录", ""]
        for record in archive[::-1][:5]:
            lines.append(
                f"· {record.get('at', '')[:16]}  "
                f"{record.get('count', 0)} 笔 / {self._money(int(record.get('total') or 0))}"
            )
        if len(archive) > 5:
            lines.append(f"… 另有 {len(archive) - 5} 条更早的记录")
        grand = sum(int(r.get("total") or 0) for r in archive)
        lines += ["", f"累计已结算 {len(archive)} 轮 / {self._money(grand)}"]
        return "\n".join(lines)

    # ------------------------------------------------------------ 动作

    def _do_clear(self, key: str, confirmed: bool) -> str:
        data = self._load()
        group = self._group(data, key)
        bills = group["bills"]
        if not bills:
            return "当前没有未结算的账单，不需要清账。"

        if not confirmed:
            return (
                f"确认清账？\n\n"
                f"将把 {len(bills)} 笔账单（合计 {self._money(ledger.total_spent(bills))}）"
                f"归档，账本归零。\n"
                f"建议先「aa 结算」把转账方案发出去。\n\n"
                f"确认无误请回复：aa 清账 确认"
            )
        try:
            record = ledger.settle_up(group)
        except LedgerError as exc:
            return f"清账失败：{exc}"
        self._save(data)
        return (
            f"🧾 已清账\n\n"
            f"{record['count']} 笔账单（合计 {self._money(record['total'])}）已归档\n"
            f"账本归零，可以开始下一轮了\n"
            f"「aa 归档」可查看历史"
        )

    def _do_undo(self, key: str, uid: str) -> str:
        data = self._load()
        group = self._group(data, key)
        bills = group["bills"]
        if not bills:
            return "没有可撤销的账单。"
        bill = bills[-1]
        if uid not in (str(bill.get("created_by")), str(bill.get("payer"))):
            return (
                f"只能撤销自己记的或自己垫付的账单。\n"
                f"最后一笔是「{bill['desc']}」，由 "
                f"{ledger.name_of(group, bill['payer'])} 垫付。"
            )
        removed = ledger.remove_bill(group, bill["id"])
        if not removed:
            return "撤销失败，账本可能已变化，请重试。"
        self._save(data)
        return (
            f"↩️ 已撤销：{removed['desc']} {self._money(removed['amount'])}\n"
            f"当前剩余 {len(group['bills'])} 笔未结"
        )

    def _do_join(self, key: str, event) -> str:
        data = self._load()
        group = self._group(data, key)
        uid, name = _sender_id(event), _sender_name(event)
        existed = uid in group["members"]
        ledger.ensure_member(group, uid, name)
        self._save(data)
        display = ledger.name_of(group, uid)
        if existed:
            return f"你已经在账本里了（{display}），共 {len(group['members'])} 位成员。"
        return (
            f"✅ {display} 已加入账本（共 {len(group['members'])} 人）\n"
            f"从下一笔开始参与平摊。"
        )

    def _do_leave(self, key: str, event) -> str:
        data = self._load()
        group = self._group(data, key)
        uid = _sender_id(event)
        if uid not in group["members"]:
            return "你不在这个账本里。"
        net = ledger.compute_net(group).get(uid, 0)
        if net != 0:
            if net > 0:
                return f"你还有 {self._money(net)} 待收，先结清再退出吧。"
            return f"你还欠 {self._money(-net)}，先结清再退出吧。"
        name = group["members"][uid].get("name")
        del group["members"][uid]
        self._save(data)
        return f"👋 {name} 已退出账本，剩 {len(group['members'])} 位成员。"

    # ------------------------------------------------------------ AI 工具（Function Calling）
    #
    # 下面这组 @filter.llm_tool 会把记账能力注册给大模型：用户不用记命令，
    # 直接说「昨天午饭 68 我先付的」「这个月大家各花了多少」就能触发。
    # 需要模型侧开启函数调用（AstrBot 默认开启）。

    def _llm_enabled(self) -> bool:
        return bool(self.config.get("llm_tools", True))

    def _llm_off_text(self) -> str:
        return "AI 记账工具已在插件配置里关闭，改用「aa」命令吧（发「aa 帮助」看用法）。"

    def _member_names(self, group: dict) -> str:
        names = [
            str((m or {}).get("name") or "").strip()
            for m in (group.get("members") or {}).values()
        ]
        names = [n for n in names if n]
        return "、".join(names) if names else "（暂无成员，让用户先发「aa 加入」）"

    def _resolve_people(self, group: dict, text: str, uid: str) -> tuple[list[str], bool]:
        """把「张三、李四」或「除王五」解析成成员 id 列表，返回 (ids, 是否排除模式)。"""
        text = (text or "").strip()
        if not text:
            return [], False
        picked: list[str] = []
        # 「我 / 自己」指当前说话人
        if "我" in text or "自己" in text:
            picked.append(uid)
        for mid in parse.find_member_ids(text, group.get("members") or {}):
            if mid not in picked:
                picked.append(mid)
        return picked, parse.has_exclude_word(text)

    def _tool_record(self, event, amount, desc: str, participants: str = "", payer: str = "") -> str:
        """AI 工具落账逻辑，返回可直接展示的文本。"""
        key = event.unified_msg_origin
        data = self._load()
        group = self._group(data, key)
        uid = _sender_id(event)
        name = _sender_name(event)

        try:
            cents = int(round(float(amount) * 100))
        except Exception:
            return "金额没看懂：请传一个数字，单位是元（例如 68 或 68.5）。"
        if cents <= 0:
            return "金额要大于 0 才能记账。"
        if cents > 100_000_000:
            return "金额太大了（上限 100 万元），请确认是不是多写了几位。"

        ledger.ensure_member(group, uid, name)

        payer_id = uid
        payer_text = (payer or "").strip()
        if payer_text and payer_text not in ("我", "自己"):
            hits = parse.find_member_ids(payer_text, group["members"])
            if not hits:
                return f"没找到垫付人「{payer_text}」。当前账本成员：{self._member_names(group)}"
            payer_id = hits[0]
        # 垫付人必须是成员，否则后续结算算不出净额
        if payer_id not in group["members"]:
            ledger.ensure_member(group, payer_id, payer_text or f"用户{payer_id[-4:]}")

        picked, exclude_mode = self._resolve_people(group, participants, uid)
        members = list(group["members"])
        if exclude_mode and picked:
            participants_ids = [m for m in members if m not in picked]
        elif picked:
            participants_ids = picked
        else:
            participants_ids = members
        if not participants_ids:
            return "分摊人不能为空：别把所有人都排除掉了。"

        clean = (desc or "").strip()[:40] or "未命名支出"
        try:
            bill = ledger.add_bill(
                group,
                payer=payer_id,
                amount=cents,
                desc=clean,
                participants=participants_ids,
                created_by=uid,
            )
        except LedgerError as exc:
            return f"记账失败：{exc}"

        self._save(data)

        shares = ledger.shares_of(bill)
        each = sorted(set(shares.values()))
        each_desc = (
            f"每人 {self._money(each[0])}" if len(each) == 1
            else f"每人 {self._money(each[0])}~{self._money(each[-1])}"
        )
        names = "、".join(ledger.name_of(group, p) for p in participants_ids)
        return (
            f"✅ 已记账：{bill['desc']} {self._money(cents)}\n"
            f"👤 {ledger.name_of(group, payer_id)} 垫付 · 👥 {len(participants_ids)} 人分摊（{each_desc}）\n"
            f"   {names}\n"
            f"账本累计 {len(group['bills'])} 笔 / {self._money(ledger.total_spent(group['bills']))}"
        )

    @_llm_tool(name="aa_record_bill")
    async def tool_record_bill(
        self,
        event: AstrMessageEvent,
        amount: float,
        desc: str,
        participants: str = "",
        payer: str = "",
    ):
        """记一笔 AA 账单：某人垫付、大家平摊的消费。当用户表达「我付了/帮我记一下/这顿 AA」等消费分摊意图且能看出金额时调用。不要臆造金额，金额不明确时先问用户。

        Args:
            amount(number): 金额，单位元，例如 68 或 68.5
            desc(string): 消费内容，如「午饭」「打车」「奶茶」，不超过 20 字
            participants(string): 参与分摊的昵称，用顿号分隔，如「张三、李四」；以「除」开头表示排除某人，如「除王五」；留空表示账本全部成员
            payer(string): 垫付人昵称，留空表示当前说话人
        """
        if not self._llm_enabled():
            yield event.plain_result(self._llm_off_text())
            return
        try:
            reply = self._tool_record(event, amount, desc, participants, payer)
        except Exception as exc:
            logger.error(f"AA记账: AI 记账工具失败: {exc}")
            reply = "记账出错了，稍后再试或改用「aa 68 午饭」。"
        yield event.plain_result(reply)

    @_llm_tool(name="aa_view_ledger")
    async def tool_view_ledger(self, event: AstrMessageEvent, detail: bool = False):
        """查看当前会话的 AA 账本：概况、成员、我的净额，或最近的账单流水。用户问「账本还有多少」「谁欠谁」「最近记了啥」时调用。

        Args:
            detail(boolean): 是否列出最近的账单流水，默认 false 只看概况
        """
        if not self._llm_enabled():
            yield event.plain_result(self._llm_off_text())
            return
        key = event.unified_msg_origin
        try:
            text = self._build_list(key) if detail else self._build_overview(key, _sender_id(event))
        except Exception as exc:
            logger.error(f"AA记账: AI 查询工具失败: {exc}")
            text = "查询账本出错了，改用「aa」命令试试。"
        yield event.plain_result(text)

    @_llm_tool(name="aa_settle_plan")
    async def tool_settle_plan(self, event: AstrMessageEvent):
        """生成 AA 结算方案：算出谁该转给谁多少钱（尽量少笔数）。用户说「算一下谁该给谁」「结一下账」时调用。只给方案，不改动账本。"""
        if not self._llm_enabled():
            yield event.plain_result(self._llm_off_text())
            return
        try:
            text = self._build_settle(event.unified_msg_origin, _sender_id(event))
        except Exception as exc:
            logger.error(f"AA记账: AI 结算工具失败: {exc}")
            text = "结算出错了，改用「aa 结算」试试。"
        yield event.plain_result(text)

    @_llm_tool(name="aa_undo_bill")
    async def tool_undo_bill(self, event: AstrMessageEvent):
        """撤销账本里最后一笔账单。用户说「记错了」「刚那笔撤掉」「删掉刚才的」时调用。只能撤销最后一笔，且只有记账人或垫付人有权撤销。"""
        if not self._llm_enabled():
            yield event.plain_result(self._llm_off_text())
            return
        try:
            text = self._do_undo(event.unified_msg_origin, _sender_id(event))
        except Exception as exc:
            logger.error(f"AA记账: AI 撤销工具失败: {exc}")
            text = "撤销出错了，改用「aa 撤销」试试。"
        yield event.plain_result(text)

    # ------------------------------------------------------------ 自动记账

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """群里有人说「午饭 68 我先付了」时自动记一笔。

        只在同时满足以下条件时触发：开关打开、是群聊、发送者已在账本里、
        消息不像命令、且能明确识别出付款线索和金额。
        """
        if not self.config.get("auto_capture", True):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        if len(text) > 60:
            return

        # 别抢自己命令的活
        head = text.split(maxsplit=1)[0].lower()
        if head in _ALL_SUBS or head in ("aa", "记账", "结算", "算账"):
            return

        # 先用纯字符串规则过滤，绝大多数群消息在第一关就返回了，不碰磁盘
        if not parse.looks_like_bill(text):
            return

        key = event.unified_msg_origin
        data = self._load()
        group = (data.get("groups") or {}).get(key)
        if not isinstance(group, dict) or not group:
            return  # 这个会话还没启用账本

        uid = _sender_id(event)
        if uid not in (group.get("members") or {}):
            return  # 只有账本成员才自动记，避免路人闲聊被记账

        try:
            reply = await self._do_record(event, key, text)
        except Exception as exc:
            logger.error(f"AA记账: 自动记账失败: {exc}")
            return
        if reply.startswith("✅"):
            # 自动记账不 @ 人，安静回执即可；先标记已处理再回复
            event.stop_event()
            yield event.plain_result(reply)
