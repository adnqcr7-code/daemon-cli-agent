from src.parser import parse_items


def test_parse_items_splits_values():
    assert parse_items("alpha, beta") == ["alpha", "beta"]


def test_parse_items_handles_empty_input():
    assert parse_items("") == []
