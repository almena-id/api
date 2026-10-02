from registry_api import answers
from registry_api import field_catalog as catalog


def test_a_pattern_is_matched_as_written() -> None:
    # Anchors and an escaped `$` at the end are the pattern's own.
    item = catalog.Field(
        id="custom:price",
        type="text",
        labels={"en": "Price"},
        source="custom",
        pattern=r"^\d+\$",
    )
    assert answers.value_of(item, {}, "12$", {}, "price") == ("12$", None)
    assert answers.value_of(item, {}, "12", {}, "price") == (None, "format")
