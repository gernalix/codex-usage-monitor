from __future__ import annotations

import unittest

from codex_monitor.capsules.source_registry import implementation as registry


class SourceRegistryTests(unittest.TestCase):
    def test_keys_are_unique(self) -> None:
        keys = [item.key for item in registry.SOURCE_REGISTRY]
        self.assertEqual(len(keys), len(set(keys)))

    def test_default_sources_are_operational_not_personal_content(self) -> None:
        defaults = registry.source_specs(default_only=True)
        self.assertTrue(defaults)
        self.assertTrue(all(not item.personal_content for item in defaults))
        self.assertIn("roadmap", {item.key for item in defaults})
        self.assertIn("prompt_history", {item.key for item in defaults})
        self.assertIn("github_autosync", {item.key for item in defaults})

    def test_personal_message_archives_are_explicit_task_only(self) -> None:
        policy = registry.sharing_policy()
        explicit = set(policy["explicit_task_only"])
        self.assertTrue(
            {"whatsapp_exporter", "telegram_history", "discord_exporter", "grindr_exporter"}
            <= explicit
        )
        self.assertNotIn("prompt_history", explicit)

    def test_chatgpt_exporter_is_consumed_via_prompt_history(self) -> None:
        policy = registry.sharing_policy()
        self.assertIn("chatgpt_exporter", policy["via_derived_store"])


if __name__ == "__main__":
    unittest.main()
