"""Figures (figures.py, owner Oct 11 2026): numbers are identified by value however an outlet wrote them, and
nothing is ever rewritten. No database and no model: these tests need no fixtures."""
from nishpaksh import figures


def same(*texts):
    sets = [figures.value_set(t) for t in texts]
    return all(s == sets[0] and s for s in sets)


def test_one_figure_in_every_spelling():
    assert same("26 people died", "twenty-six people died", "Twenty six people died", "२६ people died")
    assert same("1,20,000 voters", "120,000 voters", "1.2 lakh voters", "one lakh twenty thousand voters",
                "120 thousand voters")
    assert same("205 seats", "two hundred and five seats", "two hundred five seats")
    assert same("Rs 12,000 crore", "twelve thousand crore rupees", "12000 crore", "120 billion rupees")
    assert same("25 basis points", "0.25 per cent")


def test_scales_and_abbreviations():
    assert figures.values("Rs 1.5 lakh crore") == [1.5e12]
    assert figures.values("50 cr and 12 mn and 5k") == [5e8, 1.2e7, 5000.0]
    assert figures.values("two lakh fifty thousand") == [250000.0]
    assert figures.values("a dozen and two dozen") == [12.0, 24.0]
    assert figures.values("10 lakh and two crore") == [1e6, 2e7]          # the scale words stay with their number
    assert figures.values("25 km") == [25.0]                                # "k" is a scale only as a word of its own


def test_the_old_bug_is_gone():
    # "twenty-six" used to be read as 20 and 6
    assert figures.values("twenty-six killed") == [26.0]
    assert figures.values("twenty six killed") == [26.0]
    assert 6.0 not in figures.values("twenty-six killed")
    assert figures.values("seventy five thousand") == [75000.0]


def test_what_is_not_a_figure():
    assert figures.values("the twenty-sixth person") == []                 # an order, not a count
    assert figures.values("two-thirds of voters") == []                    # a fraction
    assert figures.values("in nineteen ninety-nine and twenty twenty-six") == []   # spoken years
    assert figures.values("first, second and third") == []
    assert figures.values("Group C and Group D posts") == []               # labels, not 100 and 500


def test_roman_numerals_only_after_a_label_word():
    assert figures.values("Section IV and Phase II") == [4.0, 2.0]
    assert figures.values("Class X students") == [10.0]
    assert figures.values("World War II") == [2.0]
    assert same("Section IV", "Section 4")
    assert figures.values("I think this is it") == []


def test_ordinals_in_digits():
    assert figures.values("on the 29th") == [29.0]


def test_spoken_min_for_written_sentences():
    assert figures.values("no one came; one of them left; two men", spoken_min=figures.SPOKEN_MIN) == [2.0]
    assert figures.values("one man", spoken_min=figures.SPOKEN_MIN) == []
    assert figures.values("1 man", spoken_min=figures.SPOKEN_MIN) == [1.0]        # digits are always figures


def test_it_identifies_and_never_rewrites():
    text = "Twenty-six died, 26 were hurt and twenty six were rescued."
    found = figures.find(text)
    assert [f.text for f in found] == ["Twenty-six", "26", "twenty six"]
    assert [f.kind for f in found] == ["words", "digits", "words"]
    assert all(text[f.start:f.end] == f.text for f in found)
    assert text == "Twenty-six died, 26 were hurt and twenty six were rescued."


def test_statements_in_different_spellings_are_one_fact():
    from nishpaksh.relate import Profile, relate
    a, b, c = ("26 people were killed in the blast", "twenty-six people were killed in the blast",
               "Twenty six people were killed in the blast")
    assert Profile(a).nums == Profile(b).nums == Profile(c).nums == frozenset({26.0})
    assert relate(a, b) == "same" and relate(a, c) == "same" and relate(b, c) == "same"
    assert relate(a, "forty people were killed in the blast") == "different"


def test_a_spelling_is_not_a_dispute_but_a_different_figure_is():
    from nishpaksh import disputes
    assert disputes.candidate("26 people were killed in the blast", "twenty-six people were killed in the blast") is None
    assert disputes.candidate("26 people were killed in the blast", "forty people were killed in the blast") == "number"
    assert disputes.candidate("Rs 5 crore was seized", "five crore rupees were seized") is None


def test_written_sentence_is_checked_by_value_in_both_spellings():
    from nishpaksh.narrative import _numbers
    source = _numbers("Police said 26 people were killed.")
    assert _numbers("Twenty-six people died.", written=True) <= source
    assert not _numbers("Twenty-seven people died.", written=True) <= source     # a spelled wrong figure is caught now
    assert not _numbers("27 people died.", written=True) <= source
    assert _numbers("One of the accused was held.", written=True) <= source      # "one of" is not a figure
    assert _numbers("Rs 1.2 lakh was paid.", written=True) <= _numbers("120,000 rupees were paid")


def test_brief_sentences_compare_figures_by_value():
    from nishpaksh.recap import _nums
    assert _nums("Twenty-six died.") == _nums("26 died.")


def test_omission_check_finds_a_figure_whatever_the_outlet_wrote():
    from nishpaksh.textmatch import Text, key_tokens
    assert "26" in key_tokens("Twenty-six people were killed in Nashik")
    assert Text("26 लोग मारे गए").has("26") and Text("twenty-six people died").has("26")
    assert Text("२६ लोग").has("26")
    assert not Text("twenty-seven people died").has("26")
