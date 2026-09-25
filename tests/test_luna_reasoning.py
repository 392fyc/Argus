"""Unit tests for the narrowly scoped PR-Agent/LiteLLM Luna adapter."""

import sys
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import patch_luna_reasoning


class FakeReasoningEffort(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class LunaReasoningPatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = AsyncMock(return_value="response")
        self.handler = SimpleNamespace(acompletion=self.original)
        self.settings = SimpleNamespace(
            config=SimpleNamespace(reasoning_effort="xhigh")
        )
        self.get_settings = Mock(return_value=self.settings)
        self.runtime_patch = patch.object(
            patch_luna_reasoning,
            "_load_runtime",
            return_value=(self.handler, self.get_settings, FakeReasoningEffort),
        )
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.assertTrue(patch_luna_reasoning.apply_patch())
        self.patched = self.handler.acompletion

    async def test_luna_request_injects_validated_effort_and_allowlist(self):
        await self.patched(model=patch_luna_reasoning.TARGET_MODEL, messages=[])

        call = self.original.await_args
        self.assertEqual(call.kwargs["reasoning_effort"], "xhigh")
        self.assertEqual(call.kwargs["allowed_openai_params"], ["reasoning_effort"])
        self.get_settings.assert_called_once_with()

    async def test_luna_request_copies_and_preserves_existing_parameters(self):
        allowed = ["response_format", "metadata"]
        metadata = {"trace_id": "abc"}
        await self.patched(
            model=patch_luna_reasoning.TARGET_MODEL,
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
            model="azure/responses/gpt-6-luna-preview",
            messages=[],
            allowed_openai_params=allowed,
            custom_parameter="kept",
        )

        self.assertEqual(
            self.original.await_args.kwargs,
            {
                "model": "azure/responses/gpt-6-luna-preview",
                "messages": [],
                "allowed_openai_params": allowed,
                "custom_parameter": "kept",
            },
        )
        self.assertIs(self.original.await_args.kwargs["allowed_openai_params"], allowed)
        self.get_settings.assert_not_called()

    async def test_apply_patch_is_idempotent(self):
        patched_once = self.handler.acompletion

        self.assertFalse(patch_luna_reasoning.apply_patch())
        self.assertIs(self.handler.acompletion, patched_once)

    async def test_effort_is_read_again_for_each_luna_request(self):
        await self.patched(model=patch_luna_reasoning.TARGET_MODEL, messages=[])
        self.settings.config.reasoning_effort = "high"
        await self.patched(model=patch_luna_reasoning.TARGET_MODEL, messages=[])

        self.assertEqual(
            [call.kwargs["reasoning_effort"] for call in self.original.await_args_list],
            ["xhigh", "high"],
        )
        self.assertEqual(self.get_settings.call_count, 2)

    async def test_invalid_effort_is_rejected_before_litellm_call(self):
        self.settings.config.reasoning_effort = "unsupported"

        with self.assertRaises(ValueError):
            await self.patched(
                model=patch_luna_reasoning.TARGET_MODEL,
                messages=[],
            )

        self.original.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
