"""M3's fixed replies and conservative whole-message small-talk matching."""

from __future__ import annotations

import re

FIXED_RESPONSES = {
    "refuse": "抱歉，这个问题我暂时没法准确回答。我可以帮您查询订单和物流、办理退换货，或解答运费、退款等售后问题。",
    "handoff": "这个问题需要人工客服处理，请通过人工客服渠道联系我们。",
    "boundary": "这个操作我这边办理不了（例如退款、改地址），请联系人工客服处理。",
    "greeting": "您好，我是售后助手，可以帮您查询订单、物流，办理退换货，或解答售后政策。",
    "thanks": "不客气，还有其他问题随时找我。",
    "goodbye": "不客气，还有其他问题随时找我。",
}

# Only a complete greeting, thanks or goodbye matches. Slot confirmations are
# deliberately absent; the caller also disables this while ask_user is paused.
_WORDS = {
    "greeting": frozenset(("你好", "您好", "嗨", "哈喽", "hello", "hi", "早上好", "下午好", "晚上好")),
    "thanks": frozenset(("谢谢", "谢谢你", "谢谢您", "多谢", "感谢", "感谢你", "感谢您", "thank you", "thanks")),
    "goodbye": frozenset(("再见", "拜拜", "下次见", "bye", "goodbye")),
}
_PUNCTUATION = re.compile(r"^[\s，。！？、,.!?~～]+|[\s，。！？、,.!?~～]+$")


def smalltalk_kind(text: str) -> str | None:
    normalized = _PUNCTUATION.sub("", text).lower()
    return next((kind for kind, words in _WORDS.items() if normalized in words), None)
