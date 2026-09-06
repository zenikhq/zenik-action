"""Two changed symbols that resolve to one anchor must not post twice."""
import run_zenik


DIFF = """diff --git a/svc/Ctl.java b/svc/Ctl.java
--- a/svc/Ctl.java
+++ b/svc/Ctl.java
@@ -50,3 +50,4 @@
 context
+added line
 context
-removed line
"""


def _changed(name, start=45, end=60):
    return {"name": name, "path": "svc/Ctl.java", "start_line": start, "end_line": end,
            "kind": "class", "language": "java", "change_type": "modified"}


def _caller(via):
    return {"symbol": {"name": "cancel", "path": "svc/Booking.java", "start_line": 9,
                       "kind": "method"},
            "reason": "calls_maybe", "depth": 1, "confidence": 1.0,
            "cross_service": False, "via": [via]}


def test_same_symbol_resolved_twice_yields_one_inline_comment():
    # The pre-v0.0.5 indexer resolved two hunks in one class to the SAME class
    # symbol, giving two identical changed entries. Halcyon PR #2 posted the
    # same inline comment twice because of it.
    bundle = {"changed": [_changed("Ctl"), _changed("Ctl")],
              "impacted": [_caller("Ctl")], "tests": []}
    out = run_zenik.build_inline_candidates(bundle, None, DIFF)
    assert len(out) == 1
    assert (out[0]["path"], out[0]["line"], out[0]["side"]) == ("svc/Ctl.java", 51, "RIGHT")


def test_distinct_symbols_on_one_anchor_both_post():
    # Two genuinely different methods whose spans both cover the added line
    # are two findings, not one.
    bundle = {"changed": [_changed("availability"), _changed("nextAvailable")],
              "impacted": [_caller("availability"), _caller("nextAvailable")], "tests": []}
    assert len(run_zenik.build_inline_candidates(bundle, None, DIFF)) == 2


def test_diff_counts_are_files_added_removed():
    assert run_zenik.diff_counts(DIFF) == (1, 1, 1)
    assert run_zenik.diff_counts("") == (0, 0, 0)
