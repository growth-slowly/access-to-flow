"""Black-box specification tests for TASK-TEST-001."""

import pytest

from converter.utils.text_normalizer import normalize_identifier


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(" Customer Name ", "customer_name", id="customer-name"),
        pytest.param("USER-ID", "user_id", id="user-id"),
        pytest.param("  A   B  C  ", "a_b_c", id="repeated-spaces"),
        pytest.param("abc___def", "abc_def", id="repeated-underscores"),
        pytest.param("Price($)", "price", id="punctuation"),
        pytest.param("", "", id="empty"),
        pytest.param("---", "", id="hyphens-only"),
        pytest.param("日本語 ABC", "abc", id="non-ascii-prefix"),
    ],
)
def test_required_examples(text, expected):
    assert normalize_identifier(text) == expected


def test_mixed_whitespace_and_hyphens_form_single_separators():
    text = " \tA-\n-- B\r\nC\v-\fD "

    assert normalize_identifier(text) == "a_b_c_d"


@pytest.mark.parametrize(
    "text",
    [
        " ",
        "\t\r\n\v\f",
        "___",
        "_$- -$_",
        "日本語",
        "!@#$%^&*()",
        "\x00\x01",
    ],
)
def test_returns_empty_when_no_valid_character_remains(text):
    assert normalize_identifier(text) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("__alpha__", "alpha"),
        ("_-alpha-_", "alpha"),
        ("a_____b", "a_b"),
        ("a_$_b", "a_b"),
        ("a-@-b", "a_b"),
        ("one - _ -- two", "one_two"),
        ("___a___b___", "a_b"),
    ],
)
def test_final_result_has_collapsed_and_trimmed_underscores(text, expected):
    assert normalize_identifier(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ABCxyz", "abcxyz"),
        ("Z9_Y0", "z9_y0"),
        ("123", "123"),
        ("9Lives", "9lives"),
        ("class", "class"),
        ("A1-B2_C3", "a1_b2_c3"),
    ],
)
def test_ascii_letters_digits_and_underscores_are_handled_as_specified(text, expected):
    assert normalize_identifier(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("café", "caf"),
        ("Ångström", "ngstrm"),
        ("abc１２3", "abc3"),
        ("a—b", "ab"),
        ("foo.bar/baz", "foobarbaz"),
        ("日本語_ABC_é", "abc"),
        # Lowercasing these Unicode characters can manufacture ASCII letters.
        # They must be removed because only ASCII input letters are retained.
        ("İD", "d"),
        ("KID", "id"),
    ],
)
def test_non_ascii_and_other_invalid_characters_are_removed_not_transliterated(
    text, expected
):
    assert normalize_identifier(text) == expected


def test_unicode_whitespace_is_treated_as_whitespace():
    assert normalize_identifier("\u2003A\u00a0B\u2029") == "a_b"


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        1.5,
        b"ABC",
        bytearray(b"ABC"),
        ["ABC"],
        ("ABC",),
        {"text": "ABC"},
        {"ABC"},
        object(),
    ],
)
def test_representative_non_string_inputs_raise_type_error(value):
    with pytest.raises(TypeError):
        normalize_identifier(value)


def test_string_subclasses_are_accepted_as_strings():
    class StringSubclass(str):
        pass

    assert normalize_identifier(StringSubclass(" Sub-Class ")) == "sub_class"


def test_normalization_is_idempotent():
    once = normalize_identifier(" __Customer--($)--NAME__ ")

    assert once == "customer_name"
    assert normalize_identifier(once) == once
