"""An inline comment means "look here". Safe changes don't get one, symbols
that can't have callers aren't asked about, and the summary says what was
checked and found fine."""
from types import SimpleNamespace

import run_zenik
from report import build_report, rank_tests, _tests_list


DIFF = """diff --git a/src/main/java/com/halcyon/scheduling/Booking.java b/src/main/java/com/halcyon/scheduling/Booking.java
--- a/src/main/java/com/halcyon/scheduling/Booking.java
+++ b/src/main/java/com/halcyon/scheduling/Booking.java
@@ -10,3 +10,4 @@
 context
+added in requiredNotice
 context
 context
@@ -30,3 +30,4 @@
 context
+added in nextAvailable
 context
 context
"""
PATH = "src/main/java/com/halcyon/scheduling/Booking.java"


def _changed(name, start, end, path=PATH, change_type="modified"):
    return {"name": name, "kind": "method", "path": path, "language": "java",
            "start_line": start, "end_line": end, "change_type": change_type}


def _caller(name, via, path="src/main/java/com/halcyon/billing/Fees.java",
            reason="calls_maybe"):
    return {"symbol": {"name": name, "path": path, "start_line": 5, "kind": "method"},
            "reason": reason, "depth": 1, "confidence": 1.0,
            "cross_service": False, "via": [via]}


def _sym(name, path, change_type="modified"):
    return SimpleNamespace(name=name, path=path, change_type=change_type,
                           kind="method", language="java", start_line=1, end_line=2)


# (a) seeds ------------------------------------------------------------------
def test_added_and_test_path_symbols_are_not_impact_seeds():
    changed = [
        _sym("requiredNotice", PATH),
        _sym("telehealthIsFreeWithTwoHoursNotice",
             "src/test/java/com/halcyon/scheduling/BookingTest.java"),
        _sym("SlotView", PATH, change_type="added"),
    ]
    seeds, skipped = run_zenik.impact_seeds(changed)
    assert [s.name for s in seeds] == ["requiredNotice"]
    assert skipped == ["telehealthIsFreeWithTwoHoursNotice (test)", "SlotView (added)"]
    # The changed count is what was queried, and that is what the report says.
    bundle = {"changed": [{"name": s.name, "path": s.path, "start_line": 1,
                           "end_line": 2, "change_type": s.change_type} for s in seeds],
              "impacted": [_caller("assess", "requiredNotice")], "tests": []}
    assert "You changed **1 function**" in build_report(
        bundle=bundle, agent_result=None, outcome="reported")


def test_no_seeds_left_is_the_no_impact_path():
    seeds, skipped = run_zenik.impact_seeds([_sym("newThing", PATH, "added")])
    assert seeds == [] and len(skipped) == 1


# (b) inline gating ----------------------------------------------------------
def _two_symbol_bundle():
    return {"changed": [_changed("requiredNotice", 5, 15), _changed("nextAvailable", 25, 35)],
            "impacted": [_caller("assess", "requiredNotice"),
                         _caller("noShowFeeCents", "nextAvailable")],
            "tests": []}


def test_needs_action_false_suppresses_the_inline_comment():
    structured = {"per_symbol": [
        {"name": "requiredNotice", "needs_action": True, "note": "callers break"},
        {"name": "nextAvailable", "needs_action": False, "note": "harmless"},
    ]}
    out = run_zenik.build_inline_candidates(_two_symbol_bundle(), structured, DIFF)
    assert len(out) == 1
    assert "`requiredNotice`" in out[0]["body"]
    assert out[0]["line"] == 11


def test_no_structured_reply_keeps_every_caller_symbol_inline():
    assert len(run_zenik.build_inline_candidates(_two_symbol_bundle(), None, DIFF)) == 2


def test_unmentioned_symbol_stays_inline():
    structured = {"per_symbol": [{"name": "nextAvailable", "needs_action": False}]}
    out = run_zenik.build_inline_candidates(_two_symbol_bundle(), structured, DIFF)
    assert [c["line"] for c in out] == [11]


# (c) the "nothing to do" line ----------------------------------------------
def test_other_changes_checked_line_lists_names_and_overflows():
    names = [f"safe{i}" for i in range(8)]
    bundle = {"changed": [_changed("hot", 5, 15)] + [_changed(n, 40 + i, 40 + i)
                                                     for i, n in enumerate(names)],
              "impacted": [_caller("assess", "hot")] + [_caller(f"c{n}", n) for n in names],
              "tests": []}
    structured = {"per_symbol": [{"name": "hot", "needs_action": True, "note": "x"}]
                  + [{"name": n, "needs_action": False, "note": "ok"} for n in names]}
    body = build_report(bundle=bundle, agent_result=None, outcome="reported",
                        structured=structured, inline_posted=True)
    assert ("8 other changes checked — nothing to do: `safe0`, `safe1`, `safe2`, "
            "`safe3`, `safe4`, `safe5`, +2 more.") in body


def test_no_safe_line_when_nothing_was_safe():
    bundle = _two_symbol_bundle()
    body = build_report(bundle=bundle, agent_result=None, outcome="reported",
                        structured={"per_symbol": []}, inline_posted=True)
    assert "nothing to do" not in body


# (d) tests worth running ----------------------------------------------------
def _test_item(name, reason, depth=1):
    return {"symbol": {"name": name, "path": f"t/{name}.java", "start_line": 1},
            "reason": reason, "depth": depth, "via": ["f"]}


def test_tests_list_ranks_tested_by_first_and_caps_at_8():
    tests = ([_test_item(f"sem{i}", "semantic") for i in range(5)]
             + [_test_item("deep", "calls_maybe", depth=3),
                _test_item("direct", "tested_by"),
                _test_item("shallow", "calls_maybe", depth=1)]
             + [_test_item(f"ref{i}", "references", depth=2) for i in range(10)])
    listed, more = rank_tests(tests)
    assert len(listed) == 8
    assert listed[0]["symbol"]["name"] == "direct"
    assert listed[1]["symbol"]["name"] == "shallow"
    assert all(t["reason"] != "semantic" for t in listed)  # 3+ provable -> no guesses
    assert more == 13 - 8
    lines = _tests_list({"tests": tests})
    assert lines[-1] == "- +5 more (see the Zenik dashboard)"


def test_semantic_tests_survive_when_few_provable_ones():
    tests = [_test_item("sem", "semantic"), _test_item("direct", "tested_by")]
    listed, more = rank_tests(tests)
    assert [t["symbol"]["name"] for t in listed] == ["direct", "sem"] and more == 0


# (e) all-safe wording -------------------------------------------------------
def test_all_safe_summary_reads_as_checked_not_as_a_problem():
    bundle = _two_symbol_bundle()
    safe = {"per_symbol": [{"name": n, "needs_action": False, "note": "fine"}
                           for n in ("requiredNotice", "nextAvailable")]}
    body = build_report(bundle=bundle, agent_result=None, outcome="reported",
                        structured=safe)
    assert "**2 places depend** on this change; all checked, none need changes." in body
    assert "places depend** on it" not in body
    assert "/zenik fix" not in body
