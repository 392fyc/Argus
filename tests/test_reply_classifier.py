"""Regression tests for _judge_reply_with_llm in patch_suggestion_format.py.

These tests stub the pr_agent.* modules in sys.modules before importing
patch_suggestion_format so the function-under-test can be exercised without
the real pr-agent package, network access, or API keys.

Each test:
  1. Installs a fake LiteLLMAIHandler whose chat_completion records the
     system_prompt / user_prompt and returns a canned verdict.
  2. Installs a fake get_settings() returning a non-empty model string.
  3. Calls _judge_reply_with_llm with a realistic finding + reply fixture.
  4. Asserts the verdict AND asserts the system prompt contains the
     narrowing anchor phrases introduced to fix Issue #4 — so the test
     fails loudly if the prompt regresses to the pre-fix phrasing.
"""

import os
import sys
import types
import importlib
import pathlib

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# -------- Fake pr_agent package -----------------------------------------

class _FakeAIHandler:
    """Records calls and returns a canned verdict string.

    Test code sets _FakeAIHandler.verdict_text before invoking the function.
    The module also captures the system_prompt / user_prompt passed in.
    """

    verdict_text = "ACCEPT: default"
    last_system = None
    last_user = None
    last_model = None

    def __init__(self, *args, **kwargs):
        pass

    async def chat_completion(self, model=None, system=None, user=None,
                              temperature=None, **kwargs):
        type(self).last_system = system
        type(self).last_user = user
        type(self).last_model = model
        # patch_suggestion_format expects a 2-tuple: (text, finish_reason)
        return type(self).verdict_text, "stop"


class _FakeSettings:
    def get(self, key, default=None):
        if key == "config.model":
            return "fake-model"
        return default


def _install_fake_pr_agent(monkeypatch):
    """Inject fake pr_agent.* modules so the lazy import inside
    _judge_reply_with_llm resolves to our stubs."""
    # Reset recorders
    _FakeAIHandler.last_system = None
    _FakeAIHandler.last_user = None
    _FakeAIHandler.last_model = None

    pkg_pr_agent = types.ModuleType("pr_agent")
    pkg_algo = types.ModuleType("pr_agent.algo")
    pkg_ai = types.ModuleType("pr_agent.algo.ai_handlers")
    mod_litellm = types.ModuleType(
        "pr_agent.algo.ai_handlers.litellm_ai_handler")
    mod_config = types.ModuleType("pr_agent.config_loader")

    mod_litellm.LiteLLMAIHandler = _FakeAIHandler
    mod_config.get_settings = lambda: _FakeSettings()

    monkeypatch.setitem(sys.modules, "pr_agent", pkg_pr_agent)
    monkeypatch.setitem(sys.modules, "pr_agent.algo", pkg_algo)
    monkeypatch.setitem(sys.modules, "pr_agent.algo.ai_handlers", pkg_ai)
    monkeypatch.setitem(
        sys.modules,
        "pr_agent.algo.ai_handlers.litellm_ai_handler",
        mod_litellm,
    )
    monkeypatch.setitem(sys.modules, "pr_agent.config_loader", mod_config)


@pytest.fixture
def judge(monkeypatch):
    """Import (or reimport) patch_suggestion_format with fakes in place
    and return its _judge_reply_with_llm function."""
    _install_fake_pr_agent(monkeypatch)

    # Stub other heavy optional deps patch_suggestion_format may touch at
    # import time. It imports stdlib + `requests` lazily inside functions,
    # so a plain import should be fine, but guard anyway.
    if "patch_suggestion_format" in sys.modules:
        mod = importlib.reload(sys.modules["patch_suggestion_format"])
    else:
        mod = importlib.import_module("patch_suggestion_format")
    return mod._judge_reply_with_llm


# -------- Anchor phrases guarding the new prompt ------------------------

# Substrings that MUST be present in the new system prompt. If anyone
# reverts to the pre-fix wording these will fail.
REQUIRED_PROMPT_ANCHORS = [
    "irreducible",          # narrowing keyword from Issue #4's proposed fix
    "ONLY",                 # ESCALATE ONLY in these narrow cases
    "mere presence of a",   # explicit "trade-off presence is not enough"
    "EXPLICITLY",           # criterion (b): explicit human request
]

# Substrings that MUST NOT be present — the old broad ESCALATE criterion.
FORBIDDEN_PROMPT_SUBSTRINGS = [
    "ESCALATE if the discussion involves architecture decisions, trade-offs, or "
    "policy choices that need human judgment.",
]


def _assert_prompt_is_narrowed(system_prompt):
    assert system_prompt, "system prompt was not captured"
    haystack = system_prompt.lower()
    for anchor in REQUIRED_PROMPT_ANCHORS:
        assert anchor.lower() in haystack, (
            f"System prompt is missing required anchor phrase {anchor!r}. "
            f"Did the ESCALATE-narrowing fix regress?"
        )
    for forbidden in FORBIDDEN_PROMPT_SUBSTRINGS:
        assert forbidden not in system_prompt, (
            f"System prompt still contains forbidden pre-fix phrasing: "
            f"{forbidden!r}"
        )


# -------- Fixtures: the four required cases -----------------------------

FINDING_BOOTSTRAP = (
    "Suggest treating 'verify cache' as a mandatory step before every state "
    "writeback to prevent drift between the local cache and the upstream "
    "GitHub Project fields."
)

# Mercury PR #186 reply, paraphrased into English, preserving the four
# concrete reasons from Issue #4.
REPLY_MERCURY_186 = (
    "Disagree with making verify mandatory before every writeback. "
    "(1) gh-project-flow is a BOOTSTRAP-ONLY skill with an expected lifetime "
    "of <100 total calls and is already scheduled for deprecation at Phase 3 "
    "once the real integration lands. "
    "(2) Adding a mandatory verify roughly doubles every HTTP call, which "
    "directly conflicts with the bootstrap speed goal. "
    "(3) We already fail-fast on 'field not found' errors, which covers the "
    "specific drift failure mode this finding worries about. "
    "(4) A verify snippet already exists for high-stakes batch operations, "
    "so the mitigation the reviewer is asking for is in place where it "
    "actually matters."
)

REPLY_COST_BENEFIT = (
    "This change applies to a code path that runs at most ~50 times over the "
    "project's lifetime (it's a one-off migration helper, removed in Phase 3). "
    "The proposed optimization adds roughly 2x latency per call for those "
    "50 calls, and we already have a retry+timeout guard in place that covers "
    "the failure mode. So the cost clearly outweighs the benefit here."
)

REPLY_VAGUE_PREFERENCE = (
    "I prefer doing it this way."
)

REPLY_EXPLICIT_HUMAN = (
    "This is a product decision about whether we support legacy clients at "
    "all — I don't think we can resolve this from the diff. Flagging for "
    "maintainer review, please have the team lead decide."
)


# -------- Tests ---------------------------------------------------------

def test_cost_benefit_reply_with_mitigation_is_accepted(judge):
    _FakeAIHandler.verdict_text = (
        "ACCEPT: reply provides concrete cost estimate, bounded scope, and "
        "references an existing retry+timeout mitigation"
    )
    verdict, reason = judge(
        original_finding="Suggest adding an explicit latency-aware backoff.",
        reply_body=REPLY_COST_BENEFIT,
    )
    assert verdict == "ACCEPT", (
        f"cost-benefit reply with mitigation should ACCEPT, got {verdict}")
    assert reason  # non-empty
    _assert_prompt_is_narrowed(_FakeAIHandler.last_system)


def test_vague_preference_reply_is_not_accepted(judge):
    # A correctly-prompted LLM should REJECT (ask for reasoning) on a bare
    # preference. We model that here. The contract is: NOT ACCEPT.
    _FakeAIHandler.verdict_text = (
        "REJECT: reply states a bare preference with no technical reasoning, "
        "please explain why"
    )
    verdict, _ = judge(
        original_finding="Consider using a dict comprehension here for "
                         "clarity.",
        reply_body=REPLY_VAGUE_PREFERENCE,
    )
    assert verdict != "ACCEPT", (
        f"vague 'I prefer X' should not ACCEPT, got {verdict}")
    assert verdict in ("REJECT", "ESCALATE")
    _assert_prompt_is_narrowed(_FakeAIHandler.last_system)


def test_explicit_human_judgment_request_is_escalated(judge):
    _FakeAIHandler.verdict_text = (
        "ESCALATE: author explicitly requested maintainer review on a "
        "product-policy decision"
    )
    verdict, _ = judge(
        original_finding="Should this endpoint still support the v1 client "
                         "shape?",
        reply_body=REPLY_EXPLICIT_HUMAN,
    )
    assert verdict == "ESCALATE", (
        f"explicit human-judgment request should ESCALATE, got {verdict}")
    _assert_prompt_is_narrowed(_FakeAIHandler.last_system)


def test_mercury_pr186_bootstrap_reply_is_accepted(judge):
    """The Mercury PR #186 regression case from Issue #4.

    The reply gives four concrete technical reasons (bounded lifetime,
    cost estimate, existing fail-fast, existing mitigation). A
    correctly-narrowed classifier must ACCEPT this, not ESCALATE it.
    """
    _FakeAIHandler.verdict_text = (
        "ACCEPT: bounded lifetime (<100 calls, deprecated at Phase 3), "
        "concrete cost estimate (~2x per HTTP call), existing fail-fast on "
        "'field not found', and existing verify snippet for high-stakes "
        "batches together form a dominant technical argument"
    )
    verdict, reason = judge(
        original_finding=FINDING_BOOTSTRAP,
        reply_body=REPLY_MERCURY_186,
    )
    assert verdict == "ACCEPT", (
        f"Mercury PR #186 reply must ACCEPT (Issue #4 regression), "
        f"got {verdict}: {reason}"
    )
    _assert_prompt_is_narrowed(_FakeAIHandler.last_system)

    # Extra guard: make sure the actual reply text reached the LLM.
    assert "BOOTSTRAP-ONLY" in (_FakeAIHandler.last_user or "")
    assert "<100" in (_FakeAIHandler.last_user or "")


def test_parser_strips_verdict_prefix_and_colon(judge):
    """Smoke test: the verdict parser handles 'VERDICT: reason' shape."""
    _FakeAIHandler.verdict_text = "ACCEPT:  trimmed reason here  "
    verdict, reason = judge(
        original_finding="x",
        reply_body="y",
    )
    assert verdict == "ACCEPT"
    assert reason == "trimmed reason here"
