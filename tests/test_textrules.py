"""Unit tests: tokenisation, phrase rules, Jaccard, candidate filters.

Run: python3 -m tests.test_textrules
"""

from __future__ import annotations

import random
import sys

from tests.harness import approx, contains, eq, ok, run_cases
from corpuslake import indexing, textrules


def test_tokeniser_ascii():
    toks = textrules.tokenize("Hello, world! 42 apples")
    eq(toks, ["hello", "world", "42", "apples"])
    # punctuation is a separator and splits words
    eq(textrules.tokenize("step-free access"), ["step", "free", "access"])
    # digits and words are distinct token classes
    eq(textrules.tokenize("abc123"), ["abc", "123"])


def test_tokeniser_unicode_and_case():
    eq(textrules.tokenize("CAFÉ au lait"), ["café", "au", "lait"])
    # NFKC: full-width digits and letters fold to ASCII
    eq(textrules.tokenize("ＡＢＣ１２３"), ["abc", "123"])
    # each CJK ideograph becomes its own token
    eq(textrules.tokenize("城市轨道"), ["城", "市", "轨", "道"])
    # mixed CJK + latin keeps both
    eq(textrules.tokenize("New 城市 line"), ["new", "城", "市", "line"])


def test_token_spans_cover_original():
    text = "  Hello,\r\nWORLD!   City 城市。 End"
    tokens = textrules.tokenize_spans(text)
    eq([t.text for t in tokens], ["hello", "world", "city", "城", "市", "end"])
    for t in tokens:
        ok(0 <= t.start < t.end <= len(text), "span inside text: %r" % (t,))
        if t.text.isascii():
            eq(text[t.start:t.end].lower(), t.text, "span matches text for %r" % (t,))
    # CRLF is folded; the token after it starts after the newline
    idx = [t.text for t in tokens].index("world")
    eq(text[tokens[idx].start:tokens[idx].end], "WORLD")


def test_phrase_slicing_and_dedup():
    toks = ["a", "b", "c", "d"]
    got = {textrules.phrase_key_display(k): v for k, v in
           textrules.slice_phrases(toks, 2, [False, False, False, True])}
    eq(sorted(got), ["a b", "b c", "c d"])
    eq(sum(got.values()), 3)
    # duplicate collapse
    toks = ["x", "y", "x", "y"]
    got = {textrules.phrase_key_display(k): v for k, v in
           textrules.slice_phrases(toks, 2, [False, False, False, True])}
    eq(got, {"x y": 2, "y x": 1})
    # n larger than the token list yields an empty set, not an error
    eq(textrules.slice_phrases(["a"], 3), [])
    # n out of range is an error
    try:
        textrules.slice_phrases(["a"], 0)
    except ValueError:
        pass
    else:
        raise AssertionError("n=0 should be rejected")


def test_phrase_sentence_span():
    toks = textrules.tokenize_spans("one two three. four five six")
    keys = {textrules.phrase_key_display(k) for k, _ in
            textrules.slice_phrases_spans(toks, 3)}
    ok("one two three" in keys, "in-span phrase present")
    ok("three four five" not in keys, "phrase must not cross a sentence break")
    ok("four five six" in keys)


def test_jaccard_basic():
    sim, inter, union = indexing.jaccard_from_sets(["a", "b"], ["b", "c"])
    approx(sim, 1 / 3)
    eq((inter, union), (1, 3))
    sim, inter, union = indexing.jaccard_from_sets([], [])
    eq(sim, 0.0, "two empty documents are not similar")
    sim, _, _ = indexing.jaccard_from_sets(["a"], ["a"])
    eq(sim, 1.0)


def test_length_filter_is_a_bound():
    t = 0.6
    ok(indexing.length_compatible(6, 10, t), "6/10 exactly meets the bound")
    ok(not indexing.length_compatible(5, 10, t), "5/10 cannot reach 0.6")
    ok(not indexing.length_compatible(0, 10, t), "empty sets are never compatible")


def test_prefix_length_math():
    for s in range(1, 60):
        for t in (0.3, 0.5, 0.6, 0.8, 1.0):
            p = indexing.prefix_length(s, t)
            ok(p >= 1, "prefix length at least 1 for non-empty sets")
            ok(p <= s, "prefix length at most the set size")
            overlap = s - p + 1
            if t < 1:
                ok(overlap >= 1, "at least one indexed element must be forced")


def _random_sets(rng, n_sets=24, max_len=40, vocab=30):
    sets = {}
    for i in range(n_sets):
        size = rng.randint(0, max_len)
        sets["d%02d" % i] = [("t%02d" % rng.randrange(vocab)) for _ in range(size)]
    return sets


def test_candidates_exact_no_false_negatives():
    """The exact candidate generator must never drop an above-threshold pair."""
    rng = random.Random(20261122)
    for trial in range(25):
        sets = _random_sets(rng)
        threshold = rng.choice([0.3, 0.5, 0.6, 0.75, 0.9])
        cand = indexing.candidate_pairs_exact(sets, threshold)
        for pair in indexing.exhaustive_pairs(sets, threshold):
            if pair["above"]:
                key = (pair["doc_a"], pair["doc_b"])
                ok(key in cand, "trial %d: missed %r at t=%.2f" % (trial, key, threshold))


def test_candidates_low_threshold_stays_lossless():
    """A low threshold widens the prefix; it must never lose an exact match."""
    rng = random.Random(4242)
    sets = {}
    for i in range(60):
        noise = [("n%02d" % rng.randrange(140)) for _ in range(90)]
        shared = ["s%02d" % j for j in range(30)]
        sets["d%02d" % i] = noise + shared[:rng.randint(0, 30)]
    for threshold in (0.25, 0.4, 0.5):
        cand = indexing.candidate_pairs_exact(sets, threshold)
        truth = [p for p in indexing.exhaustive_pairs(sets, threshold) if p["above"]]
        ok(truth, "the fixture must produce matches at t=%.2f" % threshold)
        for pair in truth:
            ok((pair["doc_a"], pair["doc_b"]) in cand,
               "missed %r at t=%.2f" % ((pair["doc_a"], pair["doc_b"]), threshold))
        ok(len(cand) >= len(truth))
        # filter reasons are explicit evidence, not a black box
        eq(set(cand.values()), {"prefix+length"})


def test_candidates_exclude_empty_documents():
    sets = {"a": [], "b": [], "c": ["x", "y", "z"], "d": ["x", "y", "z"]}
    cand = indexing.candidate_pairs_exact(sets, 0.5)
    eq(list(cand), [("c", "d")])


def test_vocabulary_order_deterministic():
    sets = {"a": ["x", "y", "z"], "b": ["y", "z", "w"], "c": ["q"]}
    first = indexing.build_term_order(sets)
    second = indexing.build_term_order(dict(reversed(list(sets.items()))))
    eq(first, second)
    eq(indexing.vocabulary_digest(first), indexing.vocabulary_digest(second))
    # ascending document frequency, then key
    eq([t for t, _f, _r in first][:2], ["q", "w"])


def test_ruleset_fingerprint_changes_with_rules():
    a = textrules.ruleset_fingerprint(
        textrules.TOKENIZER_ID, textrules.PHRASES_ID, 3, textrules.VOCAB_ID)
    b = textrules.ruleset_fingerprint(
        textrules.TOKENIZER_ID, textrules.PHRASES_ID, 4, textrules.VOCAB_ID)
    ok(a != b, "phrase length must change the fingerprint")
    ok(len(a) == 24)


def test_minhash_is_labelled_approximate():
    sets = {"a": ["x", "y", "z", "w"], "b": ["x", "y", "z", "w"],
            "c": ["x", "y"], "d": ["q", "r"]}
    pairs, estimates = indexing.candidate_pairs_minhash(sets, 0.5, num_perm=64)
    ok(("a", "b") in pairs, "identical sets collide under MinHash")
    for pair, est in estimates.items():
        ok(0.0 <= est <= 1.0, "estimate in range")


CASES = [
    ("tokeniser: ascii/punctuation/classes", test_tokeniser_ascii),
    ("tokeniser: unicode, NFKC, CJK", test_tokeniser_unicode_and_case),
    ("tokeniser: spans map to original", test_token_spans_cover_original),
    ("phrases: n-grams, dedup, bounds", test_phrase_slicing_and_dedup),
    ("phrases: do not cross sentence ends", test_phrase_sentence_span),
    ("jaccard: basic and empty", test_jaccard_basic),
    ("filter: length bound", test_length_filter_is_a_bound),
    ("filter: prefix length math", test_prefix_length_math),
    ("filter: exact candidates never miss (randomised)", test_candidates_exact_no_false_negatives),
    ("filter: low threshold stays lossless and labelled", test_candidates_low_threshold_stays_lossless),
    ("filter: empty documents excluded", test_candidates_exclude_empty_documents),
    ("vocabulary: deterministic order + digest", test_vocabulary_order_deterministic),
    ("rules: fingerprint changes with rules", test_ruleset_fingerprint_changes_with_rules),
    ("minhash: approximate mode produces estimates", test_minhash_is_labelled_approximate),
]

if __name__ == "__main__":
    raise SystemExit(run_cases(CASES))
