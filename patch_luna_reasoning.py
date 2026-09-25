"""Pass configured reasoning effort through for Argus's Azure Luna route only."""

from functools import wraps


TARGET_MODEL = "azure/responses/gpt-6-luna"
_PATCH_MARKER = "_argus_luna_reasoning_patch"


def _load_runtime():
    """Load PR-Agent dependencies lazily so this patch is testable in isolation."""
    from pr_agent.algo.ai_handlers import litellm_ai_handler
    from pr_agent.algo.utils import ReasoningEffort
    from pr_agent.config_loader import get_settings

    return litellm_ai_handler, get_settings, ReasoningEffort


def apply_patch():
    """Wrap LiteLLM completion for the exact Luna model, once per process."""
    handler, get_settings, reasoning_effort_type = _load_runtime()
    original = handler.acompletion
    if getattr(original, _PATCH_MARKER, False) is True:
        return False

    @wraps(original)
    async def patched_acompletion(*args, **kwargs):
        model = kwargs.get("model", args[0] if args else None)
        if model != TARGET_MODEL:
            return await original(*args, **kwargs)

        effort = reasoning_effort_type(
            get_settings().config.reasoning_effort
        ).value
        call_kwargs = dict(kwargs)
        call_kwargs["reasoning_effort"] = effort
        allowed_openai_params = list(
            call_kwargs.get("allowed_openai_params") or []
        )
        if "reasoning_effort" not in allowed_openai_params:
            allowed_openai_params.append("reasoning_effort")
        call_kwargs["allowed_openai_params"] = allowed_openai_params
        return await original(*args, **call_kwargs)

    setattr(patched_acompletion, _PATCH_MARKER, True)
    setattr(patched_acompletion, "_argus_original_acompletion", original)
    handler.acompletion = patched_acompletion
    print("[Argus] Luna reasoning-effort compatibility patch applied")
    return True
