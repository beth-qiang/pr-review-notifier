from prn import events as ev
from prn.engine import evaluate
from prn.github import parse_pr

from .conftest import ME, node, review


def run(cfg, prev_nodes, cur_nodes, requested=(), prev_requested=()):
    """Events for a prev → cur transition. `requested` = PR ids GitHub returns for review-requested:@me."""
    def to_prs(nodes, req):
        return [parse_pr(n, requested_from_me=n["id"] in req) for n in nodes]
    prev_prs = to_prs(prev_nodes, prev_requested)
    prev_snap = ev.build_snapshot(evaluate(prev_prs, cfg, ME), prev_prs, ME)
    cur_prs = to_prs(cur_nodes, requested)
    results = evaluate(cur_prs, cfg, ME)
    return ev.diff(prev_snap, results, cur_prs, cfg, ME)


def kinds(events):
    return sorted(e.kind for e in events)


def test_new_pr_in_notify_section(cfg):
    n = node(author="alice", number=101)
    events = run(cfg, [], [n])
    assert kinds(events) == [ev.NEW_IN_SECTION]
    assert events[0].section == "Teammates Need Review"


def test_no_event_when_nothing_changed(cfg):
    n = node(author="alice", number=102)
    assert run(cfg, [n], [n]) == []


def test_new_pr_in_non_notify_section_is_silent(cfg):
    n = node(author=ME, number=103)  # My Open PRs: notify false
    assert run(cfg, [], [n]) == []


def test_move_between_sections_notifies(cfg):
    before = node(author="bob", number=104)
    after = node(author="bob", number=104, reviews=[review("erin", "APPROVED")])
    events = run(cfg, [before], [after])
    assert kinds(events) == [ev.NEW_IN_SECTION]
    assert events[0].section == "Teammates Reviewed Not Approved by Me"


def test_completing_review_removes_without_notifying(cfg):
    before = node(author="dave", number=105, req_users=[ME])
    after = node(author="dave", number=105, reviews=[review(ME, "APPROVED")])
    assert run(cfg, [before], [after], prev_requested={"PR_105"}) == []


def test_my_own_review_move_is_silent(cfg):
    before = node(author="alice", number=120)
    after = node(author="alice", number=120, reviews=[review(ME, "COMMENTED")])
    assert run(cfg, [before], [after]) == []


def test_re_request_moves_back_with_one_notification(cfg):
    reviewed = node(author="alice", number=121, reviews=[review(ME, "CHANGES_REQUESTED")])
    again = node(author="alice", number=121, reviews=[review(ME, "CHANGES_REQUESTED")], req_users=[ME])
    events = run(cfg, [reviewed], [again], requested={"PR_121"})
    assert kinds(events) == [ev.NEW_IN_SECTION, ev.RE_REVIEW]
    notes = ev.to_notifications(events)
    assert len(notes) == 1 and "re-requested" in notes[0].title
    assert "New in Teammates Need Review" in notes[0].message


def test_others_move_still_notifies(cfg):
    mine_reviewed = node(author="alice", number=122, reviews=[review(ME, "COMMENTED")])
    # someone else approves → still in Reviewed Not Approved, no move → silent
    assert run(cfg, [mine_reviewed], [node(author="alice", number=122,
                                           reviews=[review(ME, "COMMENTED"), review("erin", "APPROVED")])]) == []


def test_re_review_requested(cfg):
    reviewed = node(author="dave", number=106, reviews=[review(ME, "CHANGES_REQUESTED")])
    rerequested = node(author="dave", number=106, reviews=[review(ME, "CHANGES_REQUESTED")],
                       req_users=[ME])
    events = run(cfg, [reviewed], [rerequested], requested={"PR_106"})
    assert ev.RE_REVIEW in kinds(events)
    notes = ev.to_notifications(events)
    assert len(notes) == 1 and "re-requested" in notes[0].title


def test_my_pr_approved_and_new_review(cfg):
    before = node(author=ME, number=107)
    after = node(author=ME, number=107, decision="APPROVED", reviews=[review("alice", "APPROVED")])
    events = run(cfg, [before], [after])
    details = [e.detail for e in events]
    assert "Approved" in details
    assert any("new review from @alice" in d for d in details)
    notes = ev.to_notifications(events)
    assert len(notes) == 1  # collapsed per PR


def test_my_own_comments_and_thread_replies_dont_notify(cfg):
    before = node(author=ME, number=108)
    after = node(author=ME, number=108, comment_authors=[ME], reviews=[review(ME, "COMMENTED")])
    assert run(cfg, [before], [after]) == []


def test_others_comment_on_my_pr(cfg):
    before = node(author=ME, number=109, comment_authors=["a"])
    after = node(author=ME, number=109, comment_authors=["a", ME, "bob"])
    events = run(cfg, [before], [after])
    assert [e.detail for e in events] == ["1 new comment from @bob"]


def test_ci_transitions(cfg):
    pending = node(author=ME, number=110, ci="PENDING")
    failed = node(author=ME, number=110, ci="FAILURE")
    passed = node(author=ME, number=110, ci="SUCCESS")
    assert [e.detail for e in run(cfg, [pending], [failed])] == ["CI failed"]
    assert [e.detail for e in run(cfg, [failed], [passed])] == ["CI passed"]
    assert run(cfg, [passed], [pending]) == []


def test_toggles_disable_events(cfg):
    cfg.settings.notifications.new_in_section = False
    cfg.settings.notifications.ci_status = False
    assert run(cfg, [], [node(author="alice", number=111)]) == []
    assert run(cfg, [node(author=ME, number=112, ci="PENDING")], [node(author=ME, number=112, ci="FAILURE")]) == []
