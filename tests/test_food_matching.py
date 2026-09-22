"""Tests for food name matching: word containment, diacritics and curated precedence."""

import pytest

from nutrition_agent.models import FoodDefinition
from nutrition_agent.nutrition import (
    find_matching_food,
    food_tokens,
    is_token_subset,
    quantity_tokens,
    strip_diacritics,
)


def _food(name: str, source: str = "curated", calories: float = 100.0) -> FoodDefinition:
    return FoodDefinition(
        name=name,
        calories=calories,
        protein=10.0,
        carbs=10.0,
        fats=1.0,
        source=source,
    )


CATALOG = [
    _food("Kuřecí prsa", calories=106.0),
    _food("Máslo 82%", calories=740.0),
    _food("Prom-IN CFM Pure Performance (syrovátkový protein)", calories=386.0),
    _food("Rýže jasmínová (suchá)", calories=355.0),
    _food("Rýže bílá (suchá)", calories=350.0),
    _food("Včelí med", calories=304.0),
    _food("Šunka vepřová výběrová", calories=120.0),
]


def test_strip_diacritics_folds_czech_characters():
    """Diacritics are removed so plain-ASCII input compares equal."""
    assert strip_diacritics("máslo") == "maslo"
    assert strip_diacritics("Šunka vepřová výběrová") == "Sunka veprova vyberova"
    assert strip_diacritics("kuřecí") == "kureci"


def test_food_tokens_ignores_punctuation_and_case():
    """Tokens are lowercase words without punctuation."""
    assert food_tokens("Máslo 82%") == {"maslo", "82"}
    assert food_tokens("Prom-IN CFM (protein)") == {"prom", "in", "cfm", "protein"}


def test_is_token_subset_requires_every_word():
    """A subset match needs all query words, a partial overlap is not enough."""
    assert is_token_subset("máslo", "Máslo 82%") is True
    assert is_token_subset("maslo", "Máslo 82%") is True
    assert is_token_subset("protein", "Prom-IN CFM Pure Performance (syrovátkový protein)")
    assert is_token_subset("protein ryže", "Rýže jasmínová (suchá)") is False
    assert is_token_subset("", "Máslo 82%") is False


def test_short_name_matches_longer_catalog_entry():
    """'máslo' must resolve to the catalog 'Máslo 82%' instead of being rejected."""
    food, _ = find_matching_food("máslo", CATALOG)
    assert food.name == "Máslo 82%"


def test_diacritic_less_input_matches_catalog_entry():
    """Plain-ASCII typing resolves to the diacritic catalog entry."""
    for query, expected in (
        ("maslo", "Máslo 82%"),
        ("sunka", "Šunka vepřová výběrová"),
        ("med", "Včelí med"),
        ("kureci prsa", "Kuřecí prsa"),
    ):
        food, _ = find_matching_food(query, CATALOG)
        assert food.name == expected, query


def test_specific_product_beats_generic_entry():
    """All query words contained in a longer product name selects that product."""
    food, _ = find_matching_food("cfm protein", CATALOG)
    assert food.name == "Prom-IN CFM Pure Performance (syrovátkový protein)"


def test_diacritic_less_synonym_is_applied():
    """Synonyms are matched without diacritics too."""
    food, _ = find_matching_food("ryze", CATALOG)
    assert food.name == "Rýže jasmínová (suchá)"

    # The synonym map itself still wins for the exact short name
    assert find_matching_food("rýže", CATALOG)[0].name == "Rýže jasmínová (suchá)"


def test_curated_entry_beats_ai_estimate():
    """An estimate must never shadow a verified catalog entry."""
    foods = [
        _food("máslo", source="ai", calories=744.0),
        _food("Máslo 82%", source="curated", calories=740.0),
    ]
    food, _ = find_matching_food("máslo", foods)
    assert food.name == "Máslo 82%"
    assert food.source == "curated"


def test_ai_entry_is_used_when_no_curated_match_exists():
    """AI entries still serve foods the catalog genuinely lacks."""
    foods = [
        _food("Máslo 82%", source="curated", calories=740.0),
        _food("marmeláda", source="ai", calories=224.0),
    ]
    food, _ = find_matching_food("marmeláda", foods)
    assert food.name == "marmeláda"
    assert food.source == "ai"


def test_unknown_food_still_fails_fast():
    """Genuinely unknown foods are still rejected so they can be estimated."""
    from nutrition_agent.models import FoodNotFoundError

    with pytest.raises(FoodNotFoundError):
        find_matching_food("pneumatika", CATALOG)


def test_quantity_tokens_normalizes_percentages():
    """Comma and dot spellings of the same number compare equal."""
    assert quantity_tokens("Máslo 82%") == {"82"}
    assert quantity_tokens("Mléko 1,5 %") == {"1.5"}
    assert quantity_tokens("Kečup") == set()


def test_stated_percentage_must_agree_with_the_candidate():
    """A stated fat content is a different variant, not a typo to correct."""
    from nutrition_agent.models import FoodNotFoundError

    foods = [_food("Eidam 30% tuku v sušině")]
    assert find_matching_food("eidam 30%", foods)[0].name == "Eidam 30% tuku v sušině"
    assert find_matching_food("eidam 30", foods)[0].name == "Eidam 30% tuku v sušině"
    # Without a stated fat content the only variant is still the best guess.
    assert find_matching_food("eidam", foods)[0].name == "Eidam 30% tuku v sušině"
    with pytest.raises(FoodNotFoundError):
        find_matching_food("eidam 45%", foods)


def test_duplicate_number_lookalike_does_not_match():
    """'eidam 45%' scores high against 'eidam 30%' but describes another cheese."""
    from nutrition_agent.models import FoodNotFoundError

    foods = [_food("eidam 30%", source="ai")]
    with pytest.raises(FoodNotFoundError):
        find_matching_food("eidam 45%", foods)
