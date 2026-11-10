"""Pass the configured reasoning effort through for Argus's target model only.

The target is whatever ``config.model`` (and ``model_turbo`` / ``model_weak``)
names in configuration.toml, currently ``openai/responses/gpt-6.1-sol``.
PR-Agent 0.38.0 sends ``reasoning_effort`` only for models in its static
SUPPORT_REASONING_EFFORT_MODELS list (o3/o4-mini), and LiteLLM drops
``reasoning_effort`` for a model it does not know unless the parameter is named
in ``allowed_openai_params``. This wrapper adds both for the target model and
leaves every other model untouched.
"""

from functools import wraps


_PATCH_MARKER = "_argus_reasoning_patch"
_MODEL_KEYS = ("model", "model_turbo", "model_weak")


def _load_runtime():
    """Load PR-Agent dependencies lazily so this patch is testable in isolation."""
    from pr_agent.algo.ai_handlers import litellm_ai_handler
    from pr_agent.algo.utils import ReasoningEffort
    from pr_agent.config_loader import get_settings

    return litellm_ai_handler, get_settings, ReasoningEffort


def _target_models(settings):
    """The model strings configured as Argus's own, read at call time."""
    config = settings.config
    return {m for m in (getattr(config, key, None) for key in _MODEL_KEYS)
            if isinstance(m, str) and m}


def apply_patch():
    """Wrap LiteLLM completion for the configured target model, once per process."""
    handler, get_settings, reasoning_effort_type = _load_runtime()
    original = handler.acompletion
    if getattr(original, _PATCH_MARKER, False) is True:
        return False

    @wraps(original)
    async def patched_acompletion(*args, **kwargs):
        model = kwargs.get("model", args[0] if args else None)
        settings = get_settings()
        if model not in _target_models(settings):
            return await original(*args, **kwargs)

        effort = reasoning_effort_type(settings.config.reasoning_effort).value
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
    print("[Argus] reasoning-effort patch applied for the configured model")
    return True
