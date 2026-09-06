"""report.py: idempotent description block, agent-reply parsing, and the
summary-comment marker that makes re-runs update instead of stack."""
from report import (COMMENT_MARKER, DESC_MARKER_END, DESC_MARKER_START,
                    build_description_block, build_report, merge_description,
                    parse_agent_message, strip_description_block)


def _bundle():
    return {
        "changed": [{"name": "f", "path": "a.py", "start_line": 1,
                     "end_line": 3, "change_type": "modified"}],
        "impacted": [{"symbol": {"name": "g", "path": "svc/b.py",
                                 "start_line": 9},
                      "reason": "calls", "cross_service": True, "via": ["f"]}],
        "tests": [], "truncated": False,
    }


def test_description_merge_is_idempotent_and_preserves_author_text():
    block = build_description_block(_bundle())
    once = merge_description("My PR does X.", block)
    twice = merge_description(once, block)
    assert once == twice
    assert once.startswith("My PR does X.")
    assert once.count(DESC_MARKER_START) == 1
    assert once.count(DESC_MARKER_END) == 1
    assert strip_description_block(once) == "My PR does X."


def test_parse_agent_message_splits_prose_and_json():
    text = ("Summary here.\n\n```json\n"
            '{"per_symbol": [{"name": "f", "note": "ok"}], "overall": "fine"}'
            "\n```")
    prose, structured = parse_agent_message(text)
    assert prose == "Summary here."
    assert structured["overall"] == "fine"
    assert parse_agent_message("no block") == ("no block", None)


def test_build_report_carries_marker_and_trust_footer():
    body = build_report(bundle=_bundle(), agent_result=None, outcome="reported")
    assert body.startswith(COMMENT_MARKER)
    assert "in other parts of the codebase" in body
    assert "/zenik fix" in body
    assert "a map of your code, not the code" in body


def test_safe_pr_goes_green_and_withholds_the_fix_offer():
    """Every impacted site judged safe -> success, no /zenik fix line."""
    from report import actionable, build_check_run, check_conclusion
    bundle = _bundle()
    names = {c["name"] for c in bundle["changed"]}
    safe = {"per_symbol": [{"name": n, "needs_action": False, "note": "harmless"}
                           for n in names]}
    assert not actionable(bundle, safe)
    assert check_conclusion(bundle, "reported", safe) == "success"
    check = build_check_run(bundle, "reported", "deadbeef", safe)
    assert check["conclusion"] == "success"
    assert "none need changes" in check["output"]["title"]
    body = build_report(bundle=bundle, agent_result=None, outcome="reported",
                        structured=safe)
    assert "/zenik fix" not in body


def test_one_actionable_symbol_keeps_the_pr_neutral():
    from report import actionable, check_conclusion
    bundle = _bundle()
    names = sorted({c["name"] for c in bundle["changed"]})
    mixed = {"per_symbol": [{"name": names[0], "needs_action": True, "note": "callers break"}]
             + [{"name": n, "needs_action": False, "note": "fine"} for n in names[1:]]}
    assert actionable(bundle, mixed)
    assert check_conclusion(bundle, "reported", mixed) == "neutral"


def test_missing_needs_action_never_makes_the_check_greener():
    """An older agent reply without the key must behave like before."""
    from report import actionable, check_conclusion
    bundle = _bundle()
    legacy = {"per_symbol": [{"name": c["name"], "note": "n"} for c in bundle["changed"]]}
    assert actionable(bundle, legacy)
    assert check_conclusion(bundle, "reported", legacy) == "neutral"
    assert check_conclusion(bundle, "reported", None) == "neutral"
