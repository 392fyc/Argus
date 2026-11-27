"""Offline replays for the PR-Agent dual review entry point."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml

import argus_dual as dual
from patch_suggestion_format import build_review_body_additions


HEAD = "a" * 40
FILE = "review.py"
PATCH = "@@ -158,2 +158,3 @@\n-old\n+checked = judge()\n return result\n+exit(0)"


def draft(*claims):
    return yaml.safe_dump({"review": {
        "key_issues_to_review": [
            {"relevant_file": FILE, "start_line": 158, "end_line": 158,
             "issue_header": "Possible Bug", "issue_content": claim} for claim in claims
        ],
        "security_concerns": "No",
    }}, allow_unicode=True)


def check(candidate, *, verdict="support", line=158, aliases=()):
    return {"id": candidate["id"], "verdict": verdict, "trigger": "输入满足复现条件",
            "impact": "提交中的代码可核对这个结果",
            "evidence": [{"file": FILE, "line": line}], "same_as": list(aliases)}


@pytest.fixture
def replay(monkeypatch):
    pytest.importorskip("pr_agent")
    monkeypatch.setattr("pr_agent.algo.pr_processing.get_pr_diff", lambda *a, **k: PATCH)
    monkeypatch.setattr(dual, "_render_prompts", lambda _: ("draft system", PATCH))
    monkeypatch.setattr(dual, "_config", dual.DualConfig(enabled=True, cross_check=True))
    monkeypatch.setattr(dual, "_ai_timeout", lambda: 3)
    monkeypatch.setattr(dual, "CROSS_MIN_SECONDS", 0)
    monkeypatch.setattr(dual, "get_claude_ledger", lambda: object())
    events = []
    monkeypatch.setattr(dual, "_emit", lambda *args: events.append(args[-1]))

    def run(gpt_draft, claude_draft, gpt_vote=None, claude_vote=None, *,
            budget=True, head=HEAD):
        calls = []
        provider = SimpleNamespace(
            last_commit_id=SimpleNamespace(sha=HEAD),
            pr_num=75,
            repo=SimpleNamespace(get_pull=lambda _: SimpleNamespace(head=SimpleNamespace(sha=head))),
            diff_files=[SimpleNamespace(filename=FILE, patch=PATCH)])
        monkeypatch.setattr(dual, "_cross_budget_ready", lambda *args: budget)

        async def main():
            arrived = {"draft": asyncio.Event(), "check": asyncio.Event()}
            counts = {"draft": 0, "check": 0}

            async def arrive(stage):
                counts[stage] += 1
                if counts[stage] == 2:
                    arrived[stage].set()
                await arrived[stage].wait()

            async def initial(model, outcome):
                calls.append(("draft", model))
                await arrive("draft")
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            async def verify(model, system, user, vote):
                calls.append(("check", model, system, user))
                await arrive("check")
                payload = json.loads(user.rsplit("\nReview evidence and candidate IDs:\n", 1)[1])
                if isinstance(vote, Exception):
                    raise vote
                entries = vote(payload["candidates"]) if vote else [
                    check(candidate) for candidate in payload["candidates"]
                ]
                return json.dumps({"head_sha": payload["head_sha"], "checks": entries})

            async def gpt_chat(**kwargs):
                text = await verify("gpt", kwargs["system"], kwargs["user"], gpt_vote)
                return text, "stop"

            async def claude_chat(system, user, *args):
                return await verify("claude", system, user, claude_vote)

            monkeypatch.setattr(dual, "claude_review", claude_chat)
            reviewer = SimpleNamespace(git_provider=provider, token_handler=None,
                                       vars={"extra_instructions": "repo rules"},
                                       _get_prediction=lambda _: initial("gpt", gpt_draft),
                                       ai_handler=SimpleNamespace(chat_completion=gpt_chat))
            await dual.dual_prepare_prediction(
                reviewer, "gpt", None, lambda: initial("claude", claude_draft))
            return reviewer

        return asyncio.run(main()), calls, events[-1]

    return run


@pytest.mark.parametrize(("initial", "agree", "expected"), [
    # Mercury #822, original positional-matching defect.
    (("重复使用第一条验收回答时，第二条漏验，程序仍退出 0。",
      "脚本按位置覆写 criterion，验收项未被判断时仍能返回 pass。"), True, 1),
    # Mercury #824, a finding from one initial line still receives two checks.
    (("通过其他运行器传入付费脚本路径时需要保留检查。",), False, 1),
    # Argus #78, same background and normal exit status, distinct defects.
    (("正常退出为 0。加载配置时没有拒绝负值阈值。",
      "正常退出为 0。磁盘写满后导出逻辑留下不完整文件。"), False, 2),
], ids=["mercury-822", "mercury-824", "argus-78"])
def test_replay_known_findings(replay, initial, agree, expected):
    def verdicts(candidates):
        return [check(c, aliases=[d["id"] for d in candidates if d != c] if agree else [])
                for c in candidates]

    reviewer, calls, event = replay(draft(initial[0]), draft(*initial[1:]), verdicts, verdicts)
    assert [call[0] for call in calls] == ["draft", "draft", "check", "check"]
    assert calls[2][2:] == calls[3][2:]
    packet = json.loads(calls[2][3].rsplit("\nReview evidence and candidate IDs:\n", 1)[1])
    assert packet["head_sha"] == HEAD and packet["project_rules"] == "repo rules"
    assert {c["claim"] for c in packet["candidates"]} == set(initial)
    assert all(set(c) == {"id", "file", "lines", "severity", "claim"} for c in packet["candidates"])
    report = yaml.safe_load(reviewer.prediction)["review"]["key_issues_to_review"]
    assert len(report) == expected
    assert event["cross_state"] == "complete"
    assert (event["confirmed"], event["combined"], event["unverified"]) == (expected, int(agree), 0)
    section = dual.cross_check_section(reviewer._argus_dual, [])
    body = build_review_body_additions(report, len(report), dual_section=section, show_models=False)
    assert "交叉复核完成" in body and "Actionable comments posted" in body
    assert "仅 GPT" not in body and "仅 Claude" not in body and "**Model**" not in body
    assert all("argus_source" not in f for f in report)
    assert "pending" not in event and "confirmed_body" not in event


def test_disagreement_and_rejection(replay):
    reviewer, _, event = replay(
        draft("双方有分歧的发现", "双方驳回的发现"), draft(),
        lambda cs: [check(c, verdict="support" if "分歧" in c["claim"] else "reject") for c in cs],
        lambda cs: [check(c, verdict="reject") for c in cs])
    assert event["confirmed"] == 0 and event["refuted"] == 1 and event["unverified"] == 1
    section = dual.cross_check_section(reviewer._argus_dual, [])
    assert "待核实" in section and "双方有分歧的发现" in section
    assert "双方驳回的发现" not in section


@pytest.mark.parametrize("fault", ["timeout", "budget", "initial", "duplicate-id"])
def test_incomplete_replays_keep_evidenced_candidates(replay, fault):
    gpt_vote = None
    if fault == "duplicate-id":
        gpt_vote = lambda cs: [check(cs[0]), check(cs[0])]
    reviewer, calls, event = replay(
        draft("有代码位置的候选"), dual.LineFailed("budget") if fault == "initial" else draft(),
        gpt_vote, TimeoutError() if fault == "timeout" else None, budget=fault != "budget")
    assert event["cross_state"] == "partial" and event["confirmed"] == 0
    assert len(calls) == (2 if fault in ("budget", "initial") else 4)
    section = dual.cross_check_section(reviewer._argus_dual, [])
    assert "复核不完整" in section and "有代码位置的候选" in section


def test_duplicate_chain_and_different_anchors(replay):
    def chain(candidates):
        a, b, c = candidates
        return [check(a, aliases=[b["id"]]), check(b, aliases=[c["id"]]), check(c)]

    reviewer, _, event = replay(draft("甲", "乙"), draft("丙"), chain, chain)
    assert event["confirmed"] == 0 and event["unverified"] == 3
    assert len(reviewer._argus_dual["pending"]) == 3

    reviewer, _, event = replay(draft("两个证据位置"), draft(), None,
                                lambda cs: [check(cs[0], line=159)])
    assert event["confirmed"] == 1 and len(reviewer._argus_dual["confirmed_body"]) == 1
    assert not yaml.safe_load(reviewer.prediction)["review"]["key_issues_to_review"]


def test_no_candidates_and_stale_commit_do_not_start_second_round(replay):
    reviewer, calls, event = replay(draft(), draft())
    assert event["cross_state"] == "empty" and len(calls) == 2
    assert "未发现候选问题" in dual.cross_check_section(reviewer._argus_dual, [])

    reviewer, calls, event = replay(draft("提交已变更"), draft(), head="b" * 40)
    assert event["cross_state"] == "stale" and len(calls) == 2
    assert reviewer.prediction is None and reviewer._argus_cross_stale


def test_truncated_empty_draft_is_not_counted_as_complete(replay):
    draft_without_security = "review:\n  key_issues_to_review: []\n"
    reviewer, calls, event = replay(draft_without_security, draft())
    assert event["cross_state"] == "partial" and len(calls) == 2
    assert "复核不完整" in dual.cross_check_section(reviewer._argus_dual, [])


def test_evidence_positions_ignore_headers_deletions_and_old_hunks():
    patch = "@@ -4 +4 @@\n-removed\n+new\n@@ -7,2 +7 @@\n-removed\n still_here"
    anchored = dual.cross_diff_positions([SimpleNamespace(filename=FILE, patch=patch)])
    assert anchored == {(FILE, 4): 2, (FILE, 7): 5}


def test_cross_budget_needs_room_on_both_lines(monkeypatch):
    import argus_budget

    def ledger(spent):
        return SimpleNamespace(check=lambda: None, snapshot=lambda: (spent, None),
                               config=argus_budget.BudgetConfig(monthly_usd=1))

    monkeypatch.setattr(argus_budget, "get_ledger", lambda: ledger(0))
    monkeypatch.setattr(dual, "get_claude_ledger", lambda: ledger(0.99))
    config = dual.DualConfig(max_tokens=4096)
    assert not dual._cross_budget_ready("gpt", config, ("rules", "candidates"))
    monkeypatch.setattr(dual, "get_claude_ledger", lambda: ledger(0))
    assert dual._cross_budget_ready("gpt", config, ("rules", "candidates"))


def test_gpt_budget_adapter_caps_cross_call_and_charges_cancellation():
    from argus_budget import make_guarded_acompletion, review_completion_limits

    charges, sent = [], []
    ledger = SimpleNamespace(check=lambda: None,
                             record_timeout=lambda *a, **kw: charges.append((a, kw)))

    async def underlying(**kwargs):
        sent.append(kwargs)
        raise asyncio.CancelledError

    async def run():
        with review_completion_limits(4, 8192):
            guarded = make_guarded_acompletion(underlying, lambda: ledger)
            with pytest.raises(asyncio.CancelledError):
                await guarded(model="gpt", timeout=20)

    asyncio.run(run())
    assert sent == [{"model": "gpt", "timeout": 4, "max_tokens": 8192}]
    assert charges[0][1]["output_tokens"] == 8192


@pytest.mark.parametrize(("stale", "captured"), [(False, True), (True, True), (False, False)])
def test_publication_uses_one_source_free_review_and_rechecks_head(replay, monkeypatch, stale, captured):
    from pr_agent.config_loader import get_settings
    from pr_agent.git_providers.github_provider import GithubProvider
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.tools.pr_description import PRDescription
    from pr_agent.tools.pr_reviewer import PRReviewer
    import patch_suggestion_format as formatting

    result, _, _ = replay(draft("会发布的已核验意见"), draft())
    created, notices = [], []
    provider = result.git_provider
    provider.repo = SimpleNamespace(
        full_name="example/repo",
        get_pull=lambda _: SimpleNamespace(head=SimpleNamespace(sha="b" * 40 if stale else HEAD)))
    provider.pr = SimpleNamespace(create_review=lambda **kw: created.append(kw))

    async def upstream_run(self):
        if captured:
            self.git_provider._argus_review_body = "PR Reviewer Guide"

    for cls, names in (
        (GithubProvider, ("auto_approve", "publish_persistent_comment", "publish_comment")),
        (PRReviewer, ("__init__", "parse_incremental", "_prepare_pr_review", "run")),
        (PRCodeSuggestions, ("push_inline_code_suggestions",)),
        (PRDescription, ("run",)),
    ):
        for name in names:
            monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(PRReviewer, "run", upstream_run)
    monkeypatch.setitem(get_settings().config, "publish_output", True)
    monkeypatch.setattr(GithubProvider, "publish_comment", lambda _, body, **kw: notices.append(body))
    monkeypatch.setattr(formatting, "auto_resolve_outdated_threads", lambda *a: 0)
    monkeypatch.setattr(formatting, "_dismiss_stale_change_requests", lambda *a: 0)
    monkeypatch.setattr(formatting, "_get_github_token", lambda *_: None)
    monkeypatch.setattr(formatting.emitter, "emit", lambda *a, **k: None)
    formatting.apply_patch()

    asyncio.run(PRReviewer.run(result))
    if stale:
        assert not created and len(notices) == 1
        assert "提交已更新" in notices[0]
    else:
        assert len(created) == 1 and not notices
        public = created[0]
        assert public["commit"].sha == HEAD and public["event"] == "COMMENT"
        assert len(public["comments"]) == 1
        assert "会发布的已核验意见" in public["comments"][0]["body"]
        assert "交叉复核完成" in public["body"]
        assert "**Model**" not in public["body"] and "来源" not in public["comments"][0]["body"]


def test_check_prompt_carries_diff_without_the_yaml_review_instruction():
    candidates = {"A01": {"relevant_file": FILE, "start_line": 158, "end_line": 158,
                          "issue_header": "Possible Bug", "issue_content": "漏验"}}
    system, user = dual._cross_messages(HEAD, candidates, PATCH, "repo rules")
    assert PATCH in user and "valid YAML" not in user and "```yaml" not in user
    assert "JSON object only" in system
    packet = json.loads(user.rsplit("\nReview evidence and candidate IDs:\n", 1)[1])
    assert packet["head_sha"] == HEAD and packet["candidates"][0]["id"] == "A01"


@pytest.mark.parametrize(("reply", "status"), [
    ("{body}", "ok"),
    ("Here is the check:\n```json\n{body}\n```", "ok"),
    ("{body}\n```", "ok"),
    ('Quoting {"file": "x"} first.\n{body}', "ok"),
    ("head_sha: x\nchecks: []\n", "unparsed"),
])
def test_check_reply_tolerates_prose_and_fences(reply, status):
    candidates = {"A01": {"relevant_file": FILE, "issue_content": "漏验"}}
    body = json.dumps({"head_sha": HEAD, "checks": [check({"id": "A01"})]}, ensure_ascii=False)
    _, parsed = dual._parse_cross_checks(reply.replace("{body}", body), HEAD, candidates,
                                         lambda *_: True)
    assert parsed == status


def test_output_allowance_grows_with_candidates():
    assert dual.cross_max_tokens(1) > dual.CROSS_MAX_TOKENS
    assert dual.cross_max_tokens(100) == dual.CROSS_MAX_TOKENS_CAP


def test_security_candidate_refuted_by_both_checks_reports_no(replay):
    flagged = yaml.safe_dump({"review": {"key_issues_to_review": [],
                                         "security_concerns": "可能存在命令注入"}}, allow_unicode=True)
    reject = lambda cs: [check(c, verdict="reject") for c in cs]
    reviewer, _, event = replay(flagged, draft(), reject, reject)
    assert event["cross_state"] == "complete" and event["refuted"] == 1
    assert yaml.safe_load(reviewer.prediction)["review"]["security_concerns"] == "No"


def test_gpt_failure_with_unusable_claude_draft_fails_the_command(replay):
    with pytest.raises(RuntimeError):
        replay(RuntimeError("gpt down"), "no review here")


def test_truncated_gpt_draft_is_not_usable():
    async def chat(**_):
        return "review: {}", "length"

    async def get_prediction(_model):
        text, _ = await reviewer.ai_handler.chat_completion(model="gpt")
        return text

    handler = SimpleNamespace(chat_completion=chat)
    reviewer = SimpleNamespace(ai_handler=handler, _get_prediction=get_prediction)
    with pytest.raises(dual.LineFailed) as failed:
        asyncio.run(dual._gpt_draft(reviewer, "gpt"))
    assert failed.value.status == "max_tokens" and handler.chat_completion is chat


def test_deadline_timeout_stops_later_calls_in_the_command():
    import argus_budget

    async def slow():
        await asyncio.sleep(1)

    async def run():
        state = {"error": None, "succeeded": 0, "timed_out": False}
        token = argus_budget._run_state.set(state)
        try:
            loop = asyncio.get_running_loop()
            result = await dual._run_to_deadline(slow, loop.time() + 0.01)
        finally:
            argus_budget._run_state.reset(token)
        return result, state

    result, state = asyncio.run(run())
    assert isinstance(result, argus_budget.ModelTimeout) and state["timed_out"]
    assert dual._line_status(result) == "timeout"


def test_unchecked_security_concern_without_location_stays_pending(replay):
    flagged = yaml.safe_dump({"review": {"key_issues_to_review": [],
                                         "security_concerns": "可能存在命令注入"}}, allow_unicode=True)
    reviewer, _, event = replay(flagged, dual.LineFailed("budget"))
    assert event["cross_state"] == "partial"
    section = dual.cross_check_section(reviewer._argus_dual, [])
    assert "可能存在命令注入" in section and "缺少可定位证据" not in section


def test_publication_filters_shrink_body_and_pending_counts():
    stats = {"confirmed_body": [{"issue_header": "Minor"}, {"issue_header": "Major"}],
             "pending": [{"issue_header": "Minor"}], "confirmed": 3, "unverified": 2}
    keep = lambda items: ([f for f in items if f["issue_header"] != "Minor"],
                          [(f, "severity") for f in items if f["issue_header"] == "Minor"])
    dropped = dual.filter_cross_extras(stats, keep)
    assert len(dropped) == 2 and stats["confirmed"] == 2 and stats["unverified"] == 1
    assert stats["pending"] == [] and len(stats["confirmed_body"]) == 1


def test_truncated_gpt_draft_without_claude_reports_a_failed_command(replay):
    import argus_budget

    state = {"error": None, "succeeded": 1, "timed_out": False}
    token = argus_budget._run_state.set(state)
    try:
        with pytest.raises(dual.LineFailed):
            replay(dual.LineFailed("max_tokens"), dual.LineFailed("no_key"))
    finally:
        argus_budget._run_state.reset(token)
    assert state["succeeded"] == 0 and isinstance(state["error"], dual.LineFailed)


def test_evidence_outside_the_hunks_makes_the_verdict_uncertain(replay):
    reviewer, calls, event = replay(draft("证据引用了上下文行"), draft(),
                                    lambda cs: [check(cs[0], line=157)])
    assert len(calls) == 4 and event["cross_state"] == "complete"
    assert event["confirmed"] == 0 and len(reviewer._argus_dual["pending"]) == 1


def test_too_little_time_left_skips_the_check_round(replay, monkeypatch):
    monkeypatch.setattr(dual, "CROSS_MIN_SECONDS", 3600)
    reviewer, calls, event = replay(draft("时间不够复核"), draft())
    assert len(calls) == 2 and event["cross_status"] == ["timeout", "timeout"]
    assert "时间不够复核" in dual.cross_check_section(reviewer._argus_dual, [])


def test_incomplete_claude_draft_with_findings_is_published_when_gpt_fails(replay):
    without_security = yaml.safe_dump({"review": {"key_issues_to_review": [
        {"relevant_file": FILE, "start_line": 158, "end_line": 158,
         "issue_header": "Possible Bug", "issue_content": "缺少安全字段的草稿"}]}}, allow_unicode=True)
    reviewer, calls, event = replay(RuntimeError("gpt down"), without_security)
    assert event["cross_state"] == "partial" and len(calls) == 2
    assert "缺少安全字段的草稿" in dual.cross_check_section(reviewer._argus_dual, [])


def test_ordinary_pending_item_does_not_block_security_no(replay):
    reviewer, _, event = replay(
        draft("双方有分歧的普通问题"), draft(),
        lambda cs: [check(c) for c in cs], lambda cs: [check(c, verdict="reject") for c in cs])
    assert event["cross_state"] == "complete" and event["unverified"] == 1
    assert yaml.safe_load(reviewer.prediction)["review"]["security_concerns"] == "No"


def test_section_renders_candidate_text_as_plain_text():
    stats = {"cross_state": "complete", "cross_status": ["ok", "ok"], "confirmed_body": [],
             "pending": [{"issue_content": "见 [说明](https://example.invalid) 与 ![图](x) 和 'q'",
                          "relevant_file": FILE, "start_line": 158}], "unverified": 1}
    section = dual.cross_check_section(stats, [])
    assert "\\[说明\\]\\(https:​//example.invalid\\)" in section
    assert "](" not in section and "![" not in section
    assert "&#x27;" not in section and "'q'" in section


def test_section_neutralises_mentions_and_bare_urls():
    stats = {"cross_state": "complete", "cross_status": ["ok", "ok"], "confirmed_body": [],
             "pending": [{"issue_content": "ping @someone, see https://bare.example or www.x.example"}],
             "unverified": 1}
    section = dual.cross_check_section(stats, [])
    assert "@someone" not in section and "https://" not in section and "www." not in section
    assert "@​someone" in section
