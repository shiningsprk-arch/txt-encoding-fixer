# -*- coding: utf-8 -*-
"""文本编码检测（TXT 编码修复 / 正文查找替换 两插件共用，不依赖 MyBooks）。

检测策略（按优先级）：
1. **BOM 优先**：UTF-8 BOM / UTF-16 BOM / UTF-32 BOM 直接判定；
2. **候选编码严格解码打分**：UTF-8 / GB18030 / BIG5 逐个 strict 解码，
   以可读性评分（中文字符占比、替换符、控制字符、常见乱码区）排序；
3. **chardet 三段采样**：开头 / 中间 / 结尾各取样本，chardet 可用时参与投票；
4. **mojibake 反转链**：对首选解码结果尝试常见乱码反转
   （``text.encode(误读编码).decode(真实编码)``），可读性显著提升时采纳，
   并标记 ``mojibake=True`` 供前端提示；
5. **可读性复检**：反转结果与直解结果比较后取最优。

对外主要接口：:func:`detect_encoding`（返回检测报告 dict）、
:func:`decode_with_report`（按报告解码出最终文本）。
"""

import re

try:
    import chardet
except ImportError:  # chardet 缺失时退化为纯规则检测
    chardet = None

# 参与候选打分的编码（strict 解码）
CANDIDATE_ENCODINGS = ("utf-8", "gb18030", "big5")
# BOM → 编码
_BOM_TABLE = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

# 乱码反转链：对解码文本尝试 ``text.encode(中间编码).decode(真实编码)`` 组合，
# 可读性显著提升时采纳。常见场景：原编码字节被程序误读（如 BIG5 被按 GB18030 读）
# 后以另一种编码（多为 UTF-8）写盘。
_MOJIBAKE_PAIRS = (
    ("gb18030", "big5"),    # BIG5 字节被按 GBK/GB18030 误读
    ("gb18030", "utf-8"),   # UTF-8 字节被按 GBK/GB18030 误读
    ("big5", "utf-8"),      # UTF-8 字节被按 BIG5 误读
    ("big5", "gb18030"),    # GBK 字节被按 BIG5 误读
    ("utf-8", "gb18030"),   # GBK 字节被按 UTF-8 误读（存为乱码 UTF-8）
    ("utf-8", "big5"),      # BIG5 字节被按 UTF-8 误读
)

# 不可读字符（替换符 / 私用区 / 代理区）
_UNREADABLE_RE = re.compile(
    "[\ufffd\ufffe\uffff\ue000-\uf8ff\ud800-\udfff\ud7b0-\ud7ff]"
)
# 常见乱码字形区（GBK 误读 UTF-8 常落入拉丁-1 补充区等）
_MOJIBAKE_CHAR_RE = re.compile(
    "[\u0080-\u00ff\u0100-\u017f\u2000-\u206f\uff00-\uffef]"
)
# 控制字符（保留 \n \r \t）
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CJK_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")

SAMPLE_CHARS = 800  # 报告中的可读性样本长度


def _readability_score(text):
    """0~100 可读性评分：中文书籍文本得分应显著高于乱码结果。"""
    if not text:
        return 0.0
    length = len(text)
    sample = text[:2000]
    n = len(sample)
    if n == 0:
        return 0.0

    replace_count = len(_UNREADABLE_RE.findall(sample))
    control_count = len(_CONTROL_RE.findall(sample))
    cjk_count = len(_CJK_RE.findall(sample))
    mojibake_count = len(_MOJIBAKE_CHAR_RE.findall(sample))

    # 可读字符 = 常规字符（非替换/非控制/非乱码字形）
    readable = n - replace_count - control_count - mojibake_count
    score = 100.0 * readable / n

    # 中文书籍文本 CJK 占比应较高，加权；纯英文书亦应可读（CJK 为 0 时不惩罚）
    cjk_ratio = cjk_count / n
    if cjk_ratio > 0.1:
        score += min(15.0, cjk_ratio * 40.0)

    # 替换符密集 = 强乱码信号
    score -= min(60.0, replace_count / max(n, 1) * 500.0)
    # 控制字符（非换行制表）几乎必为乱码/二进制
    score -= min(80.0, control_count / max(n, 1) * 1000.0)
    return max(0.0, min(100.0, score))


def _strict_decode(data, encoding):
    try:
        return data.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return None


def _sample_segments(data, size=2048, count=3):
    """取开头 / 中间 / 结尾三段样本字节。"""
    if len(data) <= size:
        return [data]
    segs = [data[:size]]
    mid = len(data) // 2
    segs.append(data[mid:mid + size])
    segs.append(data[-size:])
    return segs


def _chardet_vote(data):
    """chardet 三段采样投票；返回 (encoding, confidence) 或 None。"""
    if chardet is None:
        return None
    votes = {}
    for seg in _sample_segments(data):
        try:
            guess = chardet.detect(seg)
        except Exception:
            continue
        enc = (guess.get("encoding") or "").lower()
        conf = guess.get("confidence") or 0.0
        if enc and conf > 0.3:
            votes[enc] = votes.get(enc, 0.0) + conf
    if not votes:
        return None
    best = max(votes.items(), key=lambda kv: kv[1])
    return (best[0], best[1] / len(_sample_segments(data)))


def _decode_candidates(data):
    """对候选编码逐个 strict 解码，返回 [(encoding, text, score)] 按得分降序。"""
    results = []
    for enc in CANDIDATE_ENCODINGS:
        text = _strict_decode(data, enc)
        if text is None:
            continue
        results.append((enc, text, _readability_score(text)))
    results.sort(key=lambda r: r[2], reverse=True)
    return results


def _try_mojibake_recovery(text):
    """尝试乱码反转：对解码文本尝试 ``text.encode(中间编码).decode(真实编码)``，
    取首个可读性 >= 60 的结果（组合来自 :data:`_MOJIBAKE_PAIRS`）。

    :return: (recovered_text, real_enc, score) 或 None
    """
    best = None
    for mid_enc, real_enc in _MOJIBAKE_PAIRS:
        try:
            raw = text.encode(mid_enc)
        except (UnicodeEncodeError, LookupError):
            continue
        recovered = _strict_decode(raw, real_enc)
        if recovered is None:
            continue
        score = _readability_score(recovered)
        if score >= 60:
            best = (recovered, real_enc, score)
            break
    return best


def _analyze(data):
    """内部完整分析：返回 (text, report)。

    ``text`` 是检测 / 恢复后的全文文本（BOM 剥离、mojibake 反转恢复均已应用），
    供修复链路直接使用，避免二次解码导致反转链断裂；``report`` 即
    :func:`detect_encoding` 的报告结构。
    """
    reasons = []

    if isinstance(data, str):
        data = data.encode("utf-8")

    if not data:
        return "", {"encoding": "utf-8", "confidence": 0.0, "mojibake": False,
                    "garbage": False, "sample": "", "reasons": ["空文件"]}

    # 1. BOM 优先
    for bom, enc in _BOM_TABLE:
        if data.startswith(bom):
            text = data.decode(enc, errors="replace").lstrip("\ufeff")
            reasons.append("检测到 BOM，编码确定为 %s" % enc)
            return text, {"encoding": enc, "confidence": 1.0, "mojibake": False,
                          "garbage": False, "sample": text[:SAMPLE_CHARS],
                          "reasons": reasons}

    # 2. 候选编码 strict 解码打分
    candidates = _decode_candidates(data)
    if not candidates:
        reasons.append("所有候选编码均无法严格解码，疑似二进制或混用编码")
        return data.decode("utf-8", errors="replace"), {
            "encoding": "utf-8", "confidence": 0.0, "mojibake": False,
            "garbage": True, "sample": "", "reasons": reasons}

    enc, text, score = candidates[0]
    reasons.append("候选解码：%s（可读性 %.0f/100）" % (enc, score))

    # 3. chardet 投票（作为参考依据，不覆盖 strict 打分）
    chardet_guess = _chardet_vote(data)
    if chardet_guess:
        c_enc, c_conf = chardet_guess
        reasons.append("chardet 投票：%s（%.0f%%）" % (c_enc, c_conf * 100))

    # 4. mojibake 反转链（可读性显著提升才采纳）
    mojibake = False
    if score < 75:
        recovered = _try_mojibake_recovery(text)
        if recovered is not None:
            rec_text, rec_enc, rec_score = recovered
            if rec_score > score + 15:
                text, enc, score = rec_text, rec_enc, rec_score
                mojibake = True
                reasons.append("乱码反转恢复：按 %s 重读后为 %s（可读性 %.0f/100）"
                               % (enc, rec_enc, rec_score))

    confidence = min(1.0, score / 100.0)
    return text, {
        "encoding": enc,
        "confidence": round(confidence, 2),
        "mojibake": mojibake,
        "garbage": score < 30,
        "sample": text[:SAMPLE_CHARS],
        "reasons": reasons,
    }


def detect_encoding(data):
    """检测字节流的编码并返回报告。

    :param data: 文件原始字节（str 输入会先按 UTF-8 编码）
    :return dict: ``encoding``（建议解码编码）、``confidence``（0~1）、
        ``mojibake``（是否发生乱码反转恢复）、``garbage``（疑似非文本）、
        ``sample``（可读性最好的解码文本片段）、``reasons``（检测依据列表）。
    """
    return _analyze(data)[1]


def decode_with_report(data):
    """按检测报告解码文本；返回 (text, report)。

    ``text`` 与检测阶段完全一致（BOM 剥离 / mojibake 反转恢复均已应用），
    可直接用于后续处理。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _analyze(data)


def fix_to_utf8(data):
    """修复入口：检测并转换为 UTF-8（无 BOM）字节流。

    :return: (utf8_bytes, report)
    """
    text, report = decode_with_report(data)
    return text.encode("utf-8"), report
