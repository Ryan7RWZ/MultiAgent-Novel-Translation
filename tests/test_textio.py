"""User TXT encoding detection and UTF-8 normalization regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mant.textio import (
    TextDecodingError,
    convert_text_file_to_utf8,
    decode_text_bytes,
)


@pytest.mark.parametrize(
    ("encoding", "text", "expected_encoding"),
    [
        ("utf-8", "第一章。少女推开了房门。", "utf-8"),
        ("utf-8-sig", "第一章。少女推开了房门。", "utf-8"),
        ("gb18030", "中文测试", "gb18030"),
        ("big5", "繁體中文測試", "big5"),
        ("utf-16", "中文测试", "utf-16"),
        ("utf-32", "中文测试", "utf-32"),
        ("cp1252", "Café — déjà vu", "cp1252"),
        ("cp1251", "Привет мир", "cp1251"),
        ("shift_jis", "第一章。これは長い日本語の小説です。", "cp932"),
        ("euc_jp", "第一章。少女は扉を開けました。", "euc-jp"),
        ("euc_kr", "제1장. 소녀는 문을 열었습니다.", "cp949"),
    ],
)
def test_decode_common_txt_encodings(
    encoding: str,
    text: str,
    expected_encoding: str,
) -> None:
    decoded = decode_text_bytes(text.encode(encoding), source_name="chapter.txt")

    assert decoded.text == text
    assert decoded.byte_length == len(text.encode(encoding))
    assert decoded.encoding.startswith(expected_encoding)
    assert decoded.had_bom == (encoding in {"utf-8-sig", "utf-16", "utf-32"})


def test_convert_writes_utf8_copy_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    target = tmp_path / "normalized" / "source.txt"
    text = "第一章\n这是 GB18030 编码的小说正文。\n"
    original = text.encode("gb18030")
    source.write_bytes(original)

    decoded = convert_text_file_to_utf8(source, target)

    assert decoded.text == text
    assert decoded.encoding == "gb18030"
    assert source.read_bytes() == original
    assert target.read_bytes() == text.encode("utf-8")


def test_binary_payload_is_rejected() -> None:
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"

    with pytest.raises(TextDecodingError, match="无法可靠识别|二进制"):
        decode_text_bytes(png_header, source_name="image.txt")
