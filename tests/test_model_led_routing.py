"""Closed, synthetic tests; runnable directly without the repository bootstrap.

This loader deliberately avoids service.router.__init__, which imports the
legacy router. It tests pure discovery, not Ling quality or executor safety.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import types
import unittest

_MODULE_NAME = "_wisp_model_led_pure_test_subject"
_source_path = Path(__file__).resolve().parents[1] / "service/router/model_led.py"
subject = types.ModuleType(_MODULE_NAME)
subject.__file__ = str(_source_path)
sys.modules[_MODULE_NAME] = subject
exec(compile(_source_path.read_text(), str(_source_path), "exec"), subject.__dict__)


def tool(name="get_upcoming", module="assistant_tools", **kwargs):
    return subject.ToolSpec(name=name, description="Synthetic capability", category="assistant_read",
                            parameters={"type": "object", "properties": {}},
                            module="service.tools." + module, **kwargs)


def catalog(*tools, blocked=frozenset()):
    return subject.CapabilityCatalog.build(tools or (tool(),), blocked=blocked)


class DiscoveryTests(unittest.TestCase):
    def test_default_off_and_explicit_opt_in(self):
        for value in (None, "", "0", "false", "enabled", "maybe"):
            self.assertFalse(subject.experiment_enabled(value))
        for value in ("1", "TRUE", "yes", " on "):
            self.assertTrue(subject.experiment_enabled(value))

    def test_registry_bridge_reads_metadata_without_calling_functions(self):
        def forbidden():
            self.fail("tool function must not execute during catalog discovery")
        forbidden.__module__ = "service.tools.imessage_tools"
        fake = types.SimpleNamespace(name="view_messages", description="Synthetic message read",
                                     parameters={"type": "object", "properties": {"unread_only": {"type": "boolean"}}},
                                     category="assistant_read", func=forbidden, unavailable_reason="")
        specs = subject.registry_specs({"view_messages": fake})
        self.assertEqual(specs[0].parameters, fake.parameters)
        self.assertEqual(specs[0].schema(), {"type": "function", "function": {
            "name": "view_messages", "description": fake.description, "parameters": fake.parameters}})
        self.assertEqual(subject.family_for(specs[0]), "messages_contacts")

    def test_all_admitted_tools_visible_once_in_semantic_families(self):
        tools = (tool(), tool("summarize_emails", "email_tools"),
                 tool("custom_future", "unknown_future"),
                 subject.ToolSpec("extension_read", "Synthetic MCP", {}, "mcp_read"),
                 subject.ToolSpec("custom_skill", "Synthetic skill", {}, "skill_tool"))
        c = catalog(*tools)
        names = [name for family in c.families.values() for name in family]
        self.assertEqual(sorted(names), sorted(t.name for t in tools))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(c.families["other"], ("custom_future",))
        self.assertIn("connected", c.families)
        self.assertIn("skills", c.families)
        self.assertIn("get_upcoming", c.prompt())

    def test_unavailable_and_blocked_names_have_no_catalog_or_schema_path(self):
        c = catalog(tool("update_event", unavailable_reason="Unavailable"),
                    tool("view_emails", "email_tools"), tool(),
                    blocked=frozenset({"view_emails"}))
        self.assertEqual(set(c.tools), {"get_upcoming"})
        self.assertNotIn("mail", c.families)
        self.assertNotIn("update_event", c.prompt())
        self.assertNotIn("view_emails", c.prompt())
        with self.assertRaises(subject.SelectionError):
            subject.DiscoveryState().expand({"families": ["mail"]}, c,
                                           step_offered=frozenset({subject.DISCOVERY_TOOL}))

    def test_unknown_family_never_falls_back_to_whole_registry(self):
        state = subject.DiscoveryState()
        with self.assertRaises(subject.SelectionError):
            state.expand({"families": ["invented"]}, catalog(),
                         step_offered=frozenset({subject.DISCOVERY_TOOL}))
        self.assertEqual(state.selected, ())

    def test_malformed_selectors_fail_closed(self):
        bad = [None, [], "calendar", {}, {"families": "calendar"},
               {"families": []}, {"families": [1]}, {"families": ["calendar", "calendar"]},
               {"families": ["calendar"], "execute": True},
               {"families": ["calendar"], "tools": []},
               {"families": ["calendar"], "tools": ["get_upcoming", "get_upcoming"]},
               {"families": ["calendar"], "tools": ["../../run_shell"]}]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(subject.SelectionError):
                subject.DiscoveryState().expand(args, catalog(),
                    step_offered=frozenset({subject.DISCOVERY_TOOL}))

    def test_wrong_family_tool_and_undiscovered_call_are_not_admitted(self):
        c = catalog(tool(), tool("view_emails", "email_tools"))
        state = subject.DiscoveryState()
        with self.assertRaises(subject.SelectionError):
            state.expand({"families": ["calendar"], "tools": ["view_emails"]}, c,
                         step_offered=frozenset({subject.DISCOVERY_TOOL}))
        with self.assertRaises(subject.SelectionError):
            state.expand({"families": ["calendar"]}, c,
                         step_offered=frozenset({"get_upcoming"}))
        self.assertEqual(state.selected, ())

    def test_expansion_does_not_change_current_step_snapshot(self):
        current = frozenset({subject.DISCOVERY_TOOL})
        expansion = subject.DiscoveryState().expand({"families": ["calendar"]}, catalog(),
                                                    step_offered=current)
        self.assertNotIn("get_upcoming", current)
        self.assertEqual(expansion.names, ("get_upcoming",))
        receipt = json.loads(expansion.receipt())
        self.assertFalse(receipt["source_tools_executed"])
        self.assertEqual(receipt["schema_discovery"], "ready_next_step")

    def test_second_expansion_recovers_missing_family(self):
        c = catalog(tool(), tool("view_emails", "email_tools"))
        state = subject.DiscoveryState()
        state.expand({"families": ["calendar"]}, c,
                     step_offered=frozenset({subject.DISCOVERY_TOOL}))
        expansion = state.expand({"families": ["mail"], "tools": ["view_emails"]}, c,
                                 step_offered=frozenset({subject.DISCOVERY_TOOL, "get_upcoming"}))
        self.assertEqual(expansion.names, ("get_upcoming", "view_emails"))

    def test_stale_previous_selection_is_rechecked_against_live_catalog(self):
        state = subject.DiscoveryState()
        first = catalog(tool(), tool("view_emails", "email_tools"))
        state.expand({"families": ["mail"]}, first,
                     step_offered=frozenset({subject.DISCOVERY_TOOL}))
        refreshed = catalog(tool(), tool("view_emails", "email_tools"),
                            blocked=frozenset({"view_emails"}))
        expansion = state.expand({"families": ["calendar"]}, refreshed,
                                 step_offered=frozenset({subject.DISCOVERY_TOOL}))
        self.assertEqual(expansion.names, ("get_upcoming",))

    def test_discovery_budget_counts_failed_attempts(self):
        state = subject.DiscoveryState()
        for _ in range(subject.MAX_DISCOVERIES):
            with self.assertRaises(subject.SelectionError):
                state.expand({"families": ["invented"]}, catalog(),
                             step_offered=frozenset({subject.DISCOVERY_TOOL}))
        with self.assertRaisesRegex(subject.SelectionError, "budget exhausted"):
            state.expand({"families": ["calendar"]}, catalog(),
                         step_offered=frozenset({subject.DISCOVERY_TOOL}))
        self.assertEqual(state.selected, ())

    def test_large_family_requires_explicit_narrowing_without_truncation(self):
        c = catalog(*(tool(f"fake_{i}") for i in range(25)))
        state = subject.DiscoveryState()
        with self.assertRaisesRegex(subject.SelectionError, "24 schemas"):
            state.expand({"families": ["calendar"]}, c,
                         step_offered=frozenset({subject.DISCOVERY_TOOL}))
        self.assertEqual(state.selected, ())
        expansion = state.expand({"families": ["calendar"], "tools": ["fake_3"]}, c,
                                 step_offered=frozenset({subject.DISCOVERY_TOOL}))
        self.assertEqual(expansion.names, ("fake_3",))

    def test_schema_byte_budget_uses_utf8(self):
        large = subject.ToolSpec("huge", "\u00e9" * 20000, {}, "assistant_read", "service.tools.assistant_tools")
        with self.assertRaisesRegex(subject.SelectionError, "byte budget"):
            subject.DiscoveryState().expand({"families": ["calendar"]}, catalog(large),
                step_offered=frozenset({subject.DISCOVERY_TOOL}))

    def test_reserved_or_duplicate_registry_names_rejected(self):
        for tools in ((tool(subject.DISCOVERY_TOOL),), (tool(), tool()), (tool("../unsafe"),)):
            with self.subTest(tools=tools), self.assertRaises(subject.SelectionError):
                catalog(*tools)

    def test_capability_prompt_distinguishes_clarification_from_source_evidence(self):
        prompt = catalog().prompt()
        self.assertIn("Ask a concise question", prompt)
        self.assertIn("prior assistant prose", prompt)
        self.assertIn("not read a source", prompt)
        schema = subject.discovery_schema(catalog())
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        self.assertEqual(schema["function"]["parameters"]["properties"]["families"]["items"]["enum"], ["calendar"])

    def test_conversational_approvals_remain_baseline_but_calendar_followup_does_not(self):
        for prompt in ("yes", "go ahead", "sounds good", "no", "cancel"):
            self.assertTrue(subject.continuation_requires_baseline(prompt, "Should I send it?"))
        self.assertTrue(subject.continuation_requires_baseline("messages", "Text or email?"))
        for prompt in ("and tomorrow?", "what mom said", "what is up this weej", "unread messages today"):
            self.assertFalse(subject.continuation_requires_baseline(prompt, "Your calendar today"))


if __name__ == "__main__":
    unittest.main()
