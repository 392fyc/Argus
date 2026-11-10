"""Unit tests for the narrowly scoped PR-Agent/LiteLLM reasoning-effort adapter."""

import sys
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import patch_reasoning

TARGET_MODEL = "openai/responses/gpt-6.1-sol"


class FakeReasoningEffort(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ReasoningPatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = AsyncMock(return_value="response")
        self.handler = SimpleNamespace(acompletion=self.original)
        self.settings = SimpleNamespace(
            config=SimpleNamespace(
                reasoning_effort="high",
                model=TARGET_MODEL,
                model_turbo=TARGET_MODEL,
                model_weak=TARGET_MODEL,
            )
        )
        self.get_settings = Mock(return_value=self.settings)
        self.runtime_patch = patch.object(
            patch_reasoning,
            "_load_runtime",
            return_value=(self.handler, self.get_settings, FakeReasoningEffort),
        )
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.assertTrue(patch_reasoning.apply_patch())
        self.patched = self.handler.acompletion

    async def test_target_request_injects_validated_effort_and_allowlist(self):
        await self.patched(model=TARGET_MODEL, messages=[])

        call = self.original.await_args
        self.assertEqual(call.kwargs["reasoning_effort"], "high")
        self.assertEqual(call.kwargs["allowed_openai_params"], ["reasoning_effort"])
        self.get_settings.assert_called_once_with()

    async def test_target_request_copies_and_preserves_existing_parameters(self):
        allowed = ["response_format", "metadata"]
        metadata = {"trace_id": "abc"}
        await self.patched(
            model=TARGET_MODEL,
            messages=[],
            allowed_openai_params=allowed,
            metadata=metadata,
        )

        call = self.original.await_args
        self.assertEqual(
            call.kwargs["allowed_openai_params"],
            ["response_format", "metadata", "reasoning_effort"],
        )
        self.assertIsNot(call.kwargs["allowed_openai_params"], allowed)
        self.assertEqual(allowed, ["response_format", "metadata"])
        self.assertIs(call.kwargs["metadata"], metadata)

    async def test_other_model_is_forwarded_without_modification(self):
        allowed = ["response_format"]
        await self.patched(
            model="openai/responses/gpt-6.1-sol-preview",
            messages=[],
            allowed_openai_params=allowed,
            custom_parameter="kept",
        )

        self.assertEqual(
            self.original.await_args.kwargs,
            {
                "model": "openai/responses/gpt-6.1-sol-preview",
                "messages": [],
                "allowed_openai_params": allowed,
                "custom_parameter": "kept",
            },
        )
        self.assertIs(self.original.await_args.kwargs["allowed_openai_params"], allowed)

    async def test_model_is_read_from_configuration_not_hardcoded(self):
        self.settings.config.model = "openai/responses/other-model"
        self.settings.config.model_turbo = "openai/responses/other-model"
        self.settings.config.model_weak = "openai/responses/other-model"

        await self.patched(model=TARGET_MODEL, messages=[])
        self.assertNotIn("reasoning_effort", self.original.await_args.kwargs)
        await self.patched(model="openai/responses/other-model", messages=[])
        self.assertEqual(self.original.await_args.kwargs["reasoning_effort"], "high")

    async def test_old_azure_luna_route_is_no_longer_special(self):
        await self.patched(model="azure/responses/gpt-6-luna", messages=[])
        self.assertNotIn("reasoning_effort", self.original.await_args.kwargs)

    async def test_apply_patch_is_idempotent(self):
        patched_once = self.handler.acompletion

        self.assertFalse(patch_reasoning.apply_patch())
        self.assertIs(self.handler.acompletion, patched_once)

    async def test_effort_is_read_again_for_each_target_request(self):
        await self.patched(model=TARGET_MODEL, messages=[])
        self.settings.config.reasoning_effort = "low"
        await self.patched(model=TARGET_MODEL, messages=[])

        self.assertEqual(
            [call.kwargs["reasoning_effort"] for call in self.original.await_args_list],
            ["high", "low"],
        )
        self.assertEqual(self.get_settings.call_count, 2)

    async def test_invalid_effort_is_rejected_before_litellm_call(self):
        self.settings.config.reasoning_effort = "unsupported"

        with self.assertRaises(ValueError):
            await self.patched(
                model=TARGET_MODEL,
                messages=[],
            )

        self.original.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
