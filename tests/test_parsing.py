# -*- coding: utf-8 -*-
from app.graph import parse_catalog, parse_numbers, split_refs


def test_split_refs_separators_and_dedupe():
    assert split_refs("A;B|C，D、E F") == ["A", "B", "C", "D", "E", "F"]
    assert split_refs("A;A;;B") == ["A", "B"]
    assert split_refs(None) == []


def test_parse_numbers_supports_comments_and_separators():
    text = "# 撤下名单\nA3\nA4, A5；A6\n"
    assert parse_numbers(text) == ["A3", "A4", "A5", "A6"]


def test_parse_standard_header_semicolon():
    text = "图版号,题名,引用号\nA-1,彩陶,A-2;A-3\nA-2,无引用,\n"
    result = parse_catalog(text)
    assert result.ok, result.errors
    assert [(r.number, r.title, r.refs) for r in result.rows] == [
        ("A-1", "彩陶", ["A-2", "A-3"]),
        ("A-2", "无引用", []),
    ]


def test_parse_quoted_comma_refs():
    text = '图版号,题名,引用号\nA,题,"B,C,D"\n'
    result = parse_catalog(text)
    assert result.ok
    assert result.rows[0].refs == ["B", "C", "D"]


def test_parse_wide_ref_columns():
    text = "图版号,题名,引用号1,引用号2,引用号3\nA,题,B,C,D\n"
    result = parse_catalog(text)
    assert result.ok
    assert result.rows[0].refs == ["B", "C", "D"]


def test_parse_headerless_positional():
    text = "A,题名,B;C\nB,乙,\n"
    result = parse_catalog(text)
    assert result.ok
    assert result.rows[0].number == "A"
    assert result.rows[0].refs == ["B", "C"]
    assert result.rows[1].title == "乙"


def test_parse_duplicate_number_last_wins_with_warning():
    text = "图版号,题名,引用号\nA,旧题,B\nA,新题,C\n"
    result = parse_catalog(text)
    assert result.ok
    assert len(result.rows) == 1
    assert result.rows[0].title == "新题"
    assert result.rows[0].refs == ["C"]
    assert result.warnings


def test_parse_missing_number_is_error():
    text = "图版号,题名,引用号\n,无号,B\n"
    result = parse_catalog(text)
    assert not result.ok
    assert "图版号为空" in result.errors[0].message


def test_parse_gbk_encoding():
    text = "图版号,题名,引用号\nTP-1,彩陶盆,TP-2\n"
    result = parse_catalog(text.encode("gb18030"))
    assert result.ok
    assert result.encoding in ("gb18030",)
    assert result.rows[0].title == "彩陶盆"
