"""與 Node JSON.stringify 對齊的 canonical。

整數與有限浮點數走同一套 ES 數字格式。無法以 IEEE-754 binary64 原樣表示的整數直接拒絕。
物件鍵依 UTF-16 碼元排序，與 JavaScript 的 sort 相同。
"""
import json
import math

_MAX_SAFE = 2 ** 53 - 1


def _js_number(value):
    if isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value):
        raise ValueError("non_finite")
    if value == 0.0:
        return "0"
    text = json.dumps(value, allow_nan=False)
    sign = ""
    if text[0] == "-":
        sign = "-"
        text = text[1:]
    if "e" in text or "E" in text:
        mantissa, exponent_text = text.lower().split("e")
        exponent = int(exponent_text)
        if "." in mantissa:
            whole, frac = mantissa.split(".")
            digits = whole + frac.rstrip("0")
        else:
            digits = mantissa
        if -6 <= exponent <= 20:
            if exponent >= 0:
                if exponent + 1 >= len(digits):
                    body = digits + ("0" * (exponent + 1 - len(digits)))
                else:
                    body = digits[:exponent + 1] + "." + digits[exponent + 1:]
            else:
                body = "0." + ("0" * (-exponent - 1)) + digits
        else:
            rest = digits[1:]
            body = digits[0] + (("." + rest) if rest else "")
            body += "e" + ("+" if exponent >= 0 else "-") + str(abs(exponent))
        return sign + body
    if text.endswith(".0"):
        return sign + text[:-2]
    return sign + text


def _js_string(value):
    return json.dumps(value, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _utf16_key(value):
    units = []
    for char in value:
        code = ord(char)
        if code > 0xFFFF:
            code -= 0x10000
            units.append(0xD800 + (code >> 10))
            units.append(0xDC00 + (code & 0x3FF))
        else:
            units.append(code)
    return tuple(units)


def canonical_json(value):
    """回傳與 gateway/lib/contract.js canonical 相同的文字。不安全整數丟 ValueError。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= _MAX_SAFE:
            return str(value)
        as_float = float(value)
        if not math.isfinite(as_float) or int(as_float) != value:
            raise ValueError("unsafe_integer")
        return _js_number(as_float)
    if isinstance(value, float):
        return _js_number(value)
    if isinstance(value, str):
        return _js_string(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(_js_string(key) + ":" + canonical_json(value[key]) for key in keys) + "}"
    raise ValueError("rejected_schema")
