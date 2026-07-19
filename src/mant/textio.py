"""TXT 输入的统一编码识别与 UTF-8 转换。

所有面向用户的 TXT 入口都应先调用本模块读取原始字节，再把返回的 Unicode
文本交给切片、翻译或清洗流程。需要落盘时统一使用 UTF-8；不要覆盖用户原文件。
"""

from __future__ import annotations

import codecs
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DecodedText",
    "TextDecodingError",
    "convert_text_file_to_utf8",
    "decode_text_bytes",
    "read_text_file",
]


class TextDecodingError(ValueError):
    """TXT 字节无法被可靠识别为文本时抛出。"""


@dataclass(frozen=True, slots=True)
class DecodedText:
    """一次 TXT 解码的文本与可审计元数据。"""

    text: str
    encoding: str
    byte_length: int
    had_bom: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 必须先匹配 UTF-32；其 BOM 以 UTF-16 BOM 字节开头。
_BOM_ENCODINGS: tuple[tuple[bytes, str, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32", "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32", "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig", "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16", "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16", "utf-16-be"),
)

_COMMON_ENCODINGS: tuple[str, ...] = (
    # 中文 TXT 的主要历史编码。GB18030 是 GBK/GB2312 的超集。
    "gb18030",
    "big5",
    "cp950",
    # 常见东亚与西文文本，供自动检测不可用或未给出候选时兜底。
    "shift_jis",
    "cp932",
    "euc_jp",
    "euc_kr",
    "cp949",
    "windows-1252",
    "windows-1251",
)


def _canonical_encoding(name: str) -> str:
    try:
        return codecs.lookup(name).name.replace("_", "-")
    except LookupError:
        return str(name).strip().lower().replace("_", "-")


def _plausible_text(text: str) -> bool:
    """拒绝明显二进制内容；不对语言或小说内容作有损清洗。"""
    if not text:
        return True
    if "\x00" in text:
        return False
    controls = sum(
        ord(char) < 32 and char not in "\t\n\r"
        for char in text
    )
    private_or_unassigned = sum(
        unicodedata.category(char) in {"Co", "Cn"}
        for char in text
    )
    return (
        controls / len(text) <= 0.01
        and private_or_unassigned / len(text) <= 0.005
    )


def _detected_encodings(raw: bytes) -> list[str]:
    """返回可信的自动检测候选；检测器缺失时安全降级。

    短的旧编码文件天然存在歧义。chardet 对中文编码的置信度通常偏
    保守，即使已正确区分 GB18030 与 Big5，因此中文编码使用独立阈值。
    """
    try:
        from chardet import detect
    except ImportError:
        return []

    try:
        result = detect(raw)
    except Exception:  # noqa: BLE001 - 检测器故障时仍尝试确定性候选
        return []
    encoding = str(result.get("encoding") or "").strip()
    confidence = float(result.get("confidence") or 0.0)
    if not encoding:
        return []
    canonical = _canonical_encoding(encoding)
    chinese_family = canonical in {
        "big5",
        "cp950",
        "gb18030",
        "gb2312",
        "gbk",
        "hz",
    }
    minimum = 0.10 if chinese_family else 0.12
    return [encoding] if confidence >= minimum else []


def decode_text_bytes(raw: bytes, *, source_name: str = "TXT 文件") -> DecodedText:
    """识别任意常见 TXT 编码并返回 Unicode 文本。

    顺序为 BOM → 严格 UTF-8 → chardet → 常见东亚/西文编码。
    全程使用严格解码；不能可靠识别时抛错，避免用 replacement character 把
    乱码静默送入 LLM。
    """
    data = bytes(raw)
    if not data:
        return DecodedText(text="", encoding="utf-8", byte_length=0)

    for bom, decoder, label in _BOM_ENCODINGS:
        if data.startswith(bom):
            try:
                text = data.decode(decoder, errors="strict")
            except UnicodeError as exc:
                raise TextDecodingError(
                    f"{source_name} 带有 {label} BOM，但正文解码失败。"
                ) from exc
            if not _plausible_text(text):
                raise TextDecodingError(f"{source_name} 包含明显的二进制控制字符。")
            return DecodedText(
                text=text,
                encoding=label,
                byte_length=len(data),
                had_bom=True,
            )

    try:
        utf8 = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        if _plausible_text(utf8):
            return DecodedText(
                text=utf8,
                encoding="utf-8",
                byte_length=len(data),
            )

    candidates = _detected_encodings(data)
    # 无 BOM 的 UTF-16/32 通常含大量 NUL；只在出现该信号时提前尝试，避免把普通
    # 双字节中文编码误当成 UTF-16。
    if data.count(b"\x00") >= max(2, len(data) // 8):
        candidates = [
            "utf-32-le",
            "utf-32-be",
            "utf-16-le",
            "utf-16-be",
            *candidates,
        ]
    candidates.extend(_COMMON_ENCODINGS)

    attempted: set[str] = set()
    for candidate in candidates:
        canonical = _canonical_encoding(candidate)
        if canonical in attempted or canonical in {"utf-8", "utf-8-sig"}:
            continue
        attempted.add(canonical)
        try:
            text = data.decode(candidate, errors="strict")
        except (LookupError, UnicodeError):
            continue
        if not _plausible_text(text):
            continue
        return DecodedText(
            text=text,
            encoding=canonical,
            byte_length=len(data),
        )

    raise TextDecodingError(
        f"无法可靠识别 {source_name} 的文本编码；请确认文件确为 TXT，"
        "或先用文本编辑器另存为 UTF-8。"
    )


def read_text_file(path: str | Path) -> DecodedText:
    """从磁盘读取 TXT 原始字节并统一解码。"""
    source = Path(path)
    return decode_text_bytes(source.read_bytes(), source_name=source.name)


def convert_text_file_to_utf8(
    source: str | Path,
    destination: str | Path,
) -> DecodedText:
    """把 TXT 写为 UTF-8 副本并返回识别结果；源文件保持不变。"""
    decoded = read_text_file(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(decoded.text)
    return decoded
