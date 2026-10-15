from prn.engine import evaluate
from prn.github import parse_pr

from .conftest import ME, node, review


def place(cfg, n, requested_from_me=False):
    pr = parse_pr(n, requested_from_me=requested_from_me)
    for res in evaluate([pr], cfg, ME):
        if res.prs:
            return res.name
    return None


def test_teammate_no_approvals_needs_review(cfg):
    assert place(cfg, node(author="alice")) == "Teammates Need Review"


def test_teammate_changes_requested_by_other_still_needs_review(cfg):
    # 0 approvals → first section wins even though others reviewed
    n = node(author="bob", reviews=[review("erin", "CHANGES_REQUESTED")])
    assert place(cfg, n) == "Teammates Need Review"


def test_my_comment_review_moves_to_reviewed_not_approved(cfg):
    for state in ("COMMENTED", "CHANGES_REQUESTED"):
        n = node(author="alice", reviews=[review(ME, state)])
        assert place(cfg, n) == "Teammates Reviewed Not Approved by Me", state


def test_thread_reply_counts_as_my_review(cfg):
    # replying in a review thread is recorded by GitHub as a COMMENTED review
    n = node(author="alice", reviews=[review("erin", "COMMENTED"), review(ME, "COMMENTED")])
    assert place(cfg, n) == "Teammates Reviewed Not Approved by Me"


def test_direct_re_request_moves_back_to_need_review(cfg):
    n = node(author="alice", reviews=[review(ME, "CHANGES_REQUESTED")], req_users=[ME])
    assert place(cfg, n, requested_from_me=True) == "Teammates Need Review"


def test_re_request_with_other_approval_stays_reviewed_not_approved(cfg):
    n = node(author="alice", reviews=[review(ME, "COMMENTED"), review("erin", "APPROVED")], req_users=[ME])
    assert place(cfg, n, requested_from_me=True) == "Teammates Reviewed Not Approved by Me"


def test_lingering_team_request_does_not_count_as_re_request(cfg):
    n = node(author="alice", reviews=[review(ME, "COMMENTED")], req_teams=["platform"])
    assert place(cfg, n, requested_from_me=True) == "Teammates Reviewed Not Approved by Me"


def test_re_request_after_i_approved_goes_to_direct_request(cfg):
    n = node(author="alice", reviews=[review(ME, "APPROVED")], req_users=[ME])
    assert place(cfg, n, requested_from_me=True) == "Direct Review Request"


def test_teammate_draft_excluded(cfg):
    assert place(cfg, node(author="alice", draft=True)) is None


def test_teammate_approved_by_other_not_by_me(cfg):
    n = node(author="bob", reviews=[review("erin", "APPROVED")], decision="APPROVED")
    assert place(cfg, n) == "Teammates Reviewed Not Approved by Me"


def test_teammate_approved_by_me_drops_out(cfg):
    n = node(author="bob", reviews=[review("erin", "APPROVED"), review(ME, "APPROVED")])
    assert place(cfg, n) is None


def test_teammate_i_commented_after_approving_still_counts_as_approved(cfg):
    n = node(author="bob",
             reviews=[review("erin", "APPROVED"), review(ME, "APPROVED"), review(ME, "COMMENTED")])
    assert place(cfg, n) is None


def test_team_review_requested(cfg):
    n = node(author="dave", req_teams=["platform"])
    assert place(cfg, n, requested_from_me=True) == "Team Review Requested"


def test_team_requested_but_not_my_team(cfg):
    # team requested, but GitHub's review-requested:@me didn't include it
    n = node(author="dave", req_teams=["other-team"])
    assert place(cfg, n, requested_from_me=False) is None


def test_direct_review_request(cfg):
    n = node(author="dave", req_users=[ME])
    assert place(cfg, n, requested_from_me=True) == "Direct Review Request"


def test_direct_and_team_prefers_direct_check(cfg):
    n = node(author="dave", req_users=[ME], req_teams=["platform"])
    # requested directly → not "team only"
    assert place(cfg, n, requested_from_me=True) == "Direct Review Request"


def test_teammate_direct_request_lands_in_first_section_order(cfg):
    # User chose to keep their order: teammates sections come before direct requests.
    n = node(author="alice", req_users=[ME])
    assert place(cfg, n, requested_from_me=True) == "Teammates Need Review"


def test_my_prs(cfg):
    assert place(cfg, node(author=ME, decision="APPROVED")) == "My Approved PRs"
    assert place(cfg, node(author=ME)) == "My Open PRs"
    assert place(cfg, node(author=ME, draft=True)) == "My Draft PRs"


def test_repo_outside_scope_ignored(cfg):
    assert place(cfg, node(author=ME, repo="acme/other")) is None


def test_bot_reviews_toggle(cfg):
    n = node(author="bob", reviews=[review("copilot", "COMMENTED", bot=True)],
             latest=[review("someone", "APPROVED")])
    # with bots counted, has_reviews_from_others is true (someone + copilot)
    assert place(cfg, n) == "Teammates Reviewed Not Approved by Me"
    n2 = node(author="bob", reviews=[review("copilot", "COMMENTED", bot=True)], latest=[])
    cfg.settings.ignore_bot_reviews = True
    # 0 approvals → Needs Review regardless; check the predicate directly
    pr = parse_pr(n2)
    assert pr.has_reviews_from_others(ME, ignore_bots=True) is False
    assert pr.has_reviews_from_others(ME, ignore_bots=False) is True


def test_ignored_reviewers_dont_count(cfg):
    rabbit_approved = node(author="bob", reviews=[review("coderabbitai", "APPROVED", bot=True)])
    rabbit_commented = node(author="bob", reviews=[review("coderabbitai", "COMMENTED", bot=True)],
                            latest=[review("erin", "APPROVED")])
    # Without the setting, CodeRabbit's approval moves the PR out of "Need Review"
    assert place(cfg, rabbit_approved) == "Teammates Reviewed Not Approved by Me"
    cfg.settings.ignored_reviewers = ["CodeRabbitAI[bot]"]  # case and [bot] suffix are normalized
    assert place(cfg, rabbit_approved) == "Teammates Need Review"
    # A real approval still counts; CodeRabbit alone isn't "reviews from others"
    assert place(cfg, rabbit_commented) == "Teammates Reviewed Not Approved by Me"
    only_rabbit = parse_pr(node(author="bob", reviews=[review("coderabbitai", "COMMENTED", bot=True)]))
    assert only_rabbit.has_reviews_from_others(ME, ignored=cfg.ignored_reviewer_logins()) is False


def test_ignored_reviewers_match_with_or_without_bot_suffix(cfg):
    # Mirrors app-workflow#35442: the only review is a CodeRabbit approval, reported with a "[bot]" suffix.
    n = node(author="bob", reviews=[review("coderabbitai[bot]", "APPROVED", bot=True)])
    for setting in (["coderabbitai"], ["coderabbitai[bot]"]):
        cfg.settings.ignored_reviewers = setting
        assert place(cfg, n) == "Teammates Need Review"


def test_labels_ci_and_age_filters(cfg):
    from prn.config import parse
    c = parse("""
defaults: {repos: [acme/web]}
sections:
  - name: Urgent failing
    filters: {labels: [Urgent], exclude_labels: [wip], ci: failure, updated_within_days: 2}
""")
    hit = node(labels=["urgent"], ci="FAILURE", updated="2026-10-14T00:00:00Z")
    from datetime import datetime, timezone
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    prs = [parse_pr(hit),
           parse_pr(node(labels=["urgent", "WIP"], ci="FAILURE")),
           parse_pr(node(labels=["urgent"], ci="SUCCESS")),
           parse_pr(node(labels=["urgent"], ci="FAILURE", updated="2026-10-01T00:00:00Z"))]
    res = evaluate(prs, c, ME, now=now)
    assert [p.number for p in res[0].prs] == [hit["number"]]


def test_hidden_section_claims_prs_but_is_left_out_of_inbox():
    from prn import store
    from prn.config import parse
    c = parse("""
defaults: {repos: [acme/web]}
sections:
  - name: Parked
    display: false
    filters: {labels: [blocked]}
  - name: Everything
    filters: {}
""")
    blocked, clean = node(labels=["blocked"]), node()
    res = evaluate([parse_pr(blocked), parse_pr(clean)], c, ME)
    assert [p.number for p in res[0].prs] == [blocked["number"]]
    store.write_inbox(res, ME, next_poll_at=None)
    inbox = store.read_inbox()
    assert [s.name for s in inbox.sections] == ["Everything"]
    assert [p.number for p in inbox.sections[0].prs] == [clean["number"]]


def test_conflicts_filter():
    from prn.config import parse
    c = parse("""
defaults: {repos: [acme/web]}
sections:
  - name: Conflicting
    filters: {conflicts: true}
  - name: Clean
    filters: {conflicts: false}
""")
    dirty, clean, unknown = node(mergeable="CONFLICTING"), node(), node(mergeable="UNKNOWN")
    res = evaluate([parse_pr(n) for n in (dirty, clean, unknown)], c, ME)
    assert [p.number for p in res[0].prs] == [dirty["number"]]
    assert [p.number for p in res[1].prs] == [clean["number"], unknown["number"]]


def test_awaiting_approval_from_requires_approval_on_latest_commit():
    from prn.config import parse
    c = parse("""
defaults: {repos: [acme/web]}
sections:
  - name: Waiting on CodeRabbit
    filters: {awaiting_approval_from: ["coderabbitai[bot]"]}
""")
    approved_head = node(reviews=[review("coderabbitai", "APPROVED", bot=True, commit="head-sha")])
    approved_old = node(reviews=[review("coderabbitai", "APPROVED", bot=True, commit="old-sha")])
    commented_head = node(reviews=[review("coderabbitai", "COMMENTED", bot=True, commit="head-sha")])
    never = node()
    res = evaluate([parse_pr(n) for n in (approved_head, approved_old, commented_head, never)], c, ME)
    assert [p.number for p in res[0].prs] == [approved_old["number"], commented_head["number"], never["number"]]


def test_ready_to_merge_filter():
    from prn.config import parse
    c = parse("""
defaults: {repos: [acme/web]}
sections:
  - name: Not Ready
    filters: {ready_to_merge: false}
  - name: Ready
    filters: {ready_to_merge: true}
""")
    clean = node(merge_state="CLEAN")
    hooks = node(merge_state="HAS_HOOKS")
    blocked = node(merge_state="BLOCKED", ci="PENDING")   # e.g. policy-bot still waiting on a human
    unstable = node(merge_state="UNSTABLE", ci="FAILURE")  # a non-required check failed
    dirty = node(merge_state="DIRTY", mergeable="CONFLICTING")
    unknown = node(merge_state="UNKNOWN")
    prs = [parse_pr(n) for n in (clean, hooks, blocked, unstable, dirty, unknown)]
    res = evaluate(prs, c, ME)
    assert [p.number for p in res[1].prs] == [clean["number"], hooks["number"]]
    assert [p.number for p in res[0].prs] == [blocked["number"], unstable["number"], dirty["number"], unknown["number"]]


def test_approved_by_requires_any_listed_approval_on_latest_commit():
    from prn.config import parse
    c = parse("""
defaults: {repos: [acme/web]}
sections:
  - name: Approved by Claire or CodeRabbit
    filters: {approved_by: [claire, "coderabbitai[bot]"]}
""")
    bot_head = node(reviews=[review("coderabbitai", "APPROVED", bot=True, commit="head-sha")])
    claire_head = node(reviews=[review("claire", "APPROVED", commit="head-sha")])
    claire_old = node(reviews=[review("claire", "APPROVED", commit="old-sha")])
    claire_changes = node(reviews=[review("claire", "CHANGES_REQUESTED", commit="head-sha")])
    other_head = node(reviews=[review("bob", "APPROVED", commit="head-sha")])
    never = node()
    prs = [bot_head, claire_head, claire_old, claire_changes, other_head, never]
    res = evaluate([parse_pr(n) for n in prs], c, ME)
    assert [p.number for p in res[0].prs] == [bot_head["number"], claire_head["number"]]
