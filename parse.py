"""文本解析：金额、成员、描述清洗，以及「像不像一笔消费」的判定。

刻意不依赖 LLM——群里刷屏太快，调模型的延迟和成本都不合适；
规则能覆盖绝大部分日常说法，剩下的交给明确指令。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- 中文数字

_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
    "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}

_CN_CHARS = "".join(_DIGITS) + "".join(_UNITS)


def cn_to_int(text: str) -> int | None:
    """中文数字转整数，支持 0~9999 的口语写法。

    「六十八」-> 68、「两百三」-> 230、「一百零五」-> 105、「十」-> 10。
    无法解析返回 None。
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if not all(ch in _CN_CHARS for ch in text):
        return None

    total = 0        # 已完成结算的部分
    num = 0          # 当前待绑定单位的数字
    last_unit = 0    # 上一个使用的单位
    zero_seen = False  # 「一百零五」里的「零」标志，表示后面的数字不再进十位

    for ch in text:
        if ch in ("零", "〇"):
            zero_seen = True
            continue
        if ch in _DIGITS:
            num = _DIGITS[ch]
        elif ch in _UNITS:
            unit = _UNITS[ch]
            if num == 0 and unit == 10:
                num = 1  # 「十五」= 15
            total += num * unit
            num = 0
            last_unit = unit
            zero_seen = False

    if num:
        if last_unit >= 100 and not zero_seen:
            total += num * (last_unit // 10)  # 「两百三」的「三」是十位
        else:
            total += num
    return total


# ---------------------------------------------------------------- 金额

_UNIT_WORDS = r"(?:元钱|块钱|块|元|圆|rmb|RMB|Rmb)"
# 紧跟这些量词的数字不是金额（「3 个人」「2 份」）
_MEASURE = set("个人份瓶杯次位号楼岁天月日条张件只台节门科页包袋盒碗盘斤公斤克")

_AMOUNT_MAX = 100_000_00  # 10 万元，超过多半是误识
_AMOUNT_MIN = 50          # 0.5 元


def _to_cents(text: str) -> int:
    text = text.replace(",", "")
    if "." in text:
        yuan, fen = text.split(".", 1)
        fen = (fen + "00")[:2]
        return int(yuan or 0) * 100 + int(fen)
    return int(text) * 100


def extract_amount(text: str) -> tuple[int, str] | None:
    """从文本里找金额，返回 (金额分, 命中的原文片段)。找不到返回 None。

    优先级：货币符号 > 阿拉伯数字+单位 > 中文数字+单位 > 裸数字（排除量词）。
    """
    if not text:
        return None

    # 1) 前置符号：¥68 / ￥68.5
    m = re.search(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
    if m:
        return _to_cents(m.group(1)), m.group(0)

    # 2) 阿拉伯数字 + 单位：68元 / 68.5块 / 120块钱
    m = re.search(r"(\d+(?:\.\d{1,2})?)\s*" + _UNIT_WORDS, text)
    if m:
        return _to_cents(m.group(1)), m.group(0)

    # 3) 中文数字 + 单位：六十八块 / 三十五元
    m = re.search(r"([" + _CN_CHARS + r"]{1,6})\s*" + _UNIT_WORDS, text)
    if m:
        value = cn_to_int(m.group(1))
        if value:
            return value * 100, m.group(0)

    # 3.5) 中文数字裸用：「两百三 住宿」「六十八 晚饭」
    #      要求前后不粘汉字，且后接空白/标点，避免把「三位」「一起」当成金额
    m = re.search(
        r"(?<![\u4e00-\u9fff])([" + _CN_CHARS + r"]{2,6})(?=\s|$|[,，。.!！?？])",
        text,
    )
    if m:
        value = cn_to_int(m.group(1))
        if value and _AMOUNT_MIN <= value * 100 <= _AMOUNT_MAX:
            return value * 100, m.group(0)

    # 4) 裸数字：排除后面跟量词的（「3 个人」不是 3 元）
    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d{1,2})?)(?![\d.])", text):
        tail = text[m.end(): m.end() + 1]
        if tail and tail in _MEASURE:
            continue
        value = _to_cents(m.group(1))
        if _AMOUNT_MIN <= value <= _AMOUNT_MAX:
            return value, m.group(0)

    return None


# ---------------------------------------------------------------- 成员

def find_member_ids(text: str, members: dict) -> list[str]:
    """在文本里按名字找成员，长名优先，避免「张三」吃掉「张三丰」。"""
    if not text or not members:
        return []
    ordered = sorted(
        ((str(uid), (info or {}).get("name") or "") for uid, info in members.items()),
        key=lambda pair: -len(pair[1]),
    )
    found: list[str] = []
    rest = text
    for uid, name in ordered:
        if name and name in rest:
            found.append(uid)
            rest = rest.replace(name, " ", 1)
    return found


_EXCLUDE_WORDS = ("除", "不含", "不算", "不参与", "没来", "没参加", "去掉")


def has_exclude_word(text: str) -> bool:
    return any(word in text for word in _EXCLUDE_WORDS)


# ---------------------------------------------------------------- 描述

_PAYER_WORDS = (
    "我付", "我买", "我先付", "我垫", "垫付", "买单", "结账", "付款",
    "请客", "我出", "付了", "花了", "付的钱",
)

_HYPOTHETICAL = (
    "如果", "要是", "下次", "以后", "打算", "准备", "应该", "会不会",
    "要不要", "值不值", "便宜", "贵不贵",
)

_QUESTION_HINTS = ("多少", "几块", "多少钱", "吗", "呢", "?", "？")


def clean_desc(text: str, amount_frag: str = "", drop_names: list[str] | None = None) -> str:
    """去掉金额、@提及、成员名、付款线索词、排除词，剩下的当描述。"""
    out = text or ""
    if amount_frag:
        out = out.replace(amount_frag, " ", 1)
    # 先整体吃掉 @提及（含名字），再去名字，避免残留孤立的 @
    out = re.sub(r"[@＠]\S+", " ", out)
    out = re.sub(r"[@＠]", " ", out)
    for name in drop_names or []:
        if name:
            out = out.replace(name, " ")
    # 付款线索词连同后面的「的/了/啦」一起清掉（「我先付了」→ 空）
    payer_pattern = "|".join(
        re.escape(w) for w in sorted(_PAYER_WORDS, key=len, reverse=True)
    )
    out = re.sub(f"(?:{payer_pattern})(?:的|了|啦|呀|啊)?", " ", out)
    # 排除词（「除」「不含」等）本身也不该留在描述里
    for word in _EXCLUDE_WORDS:
        out = out.replace(word, " ")
    out = re.sub(r"[，,、。.！!~？?]+", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out.strip(" 的了").strip()[:30]


def looks_like_bill(text: str) -> bool:
    """判断一条群消息像不像「我付了多少钱」——用于自动记账。

    刻意收紧：必须有付款线索词 + 可识别金额，且不能是疑问句 / 假设句。
    宁可漏记（用户还能手动发 aa），也不要误记（污染账本更麻烦）。
    """
    text = (text or "").strip()
    if not text or len(text) > 80:
        return False
    if any(hint in text for hint in _QUESTION_HINTS):
        return False
    if any(word in text for word in _HYPOTHETICAL):
        return False
    if not any(word in text for word in _PAYER_WORDS):
        return False

    found = extract_amount(text)
    if not found:
        return False
    cents, _ = found
    return _AMOUNT_MIN <= cents <= _AMOUNT_MAX
