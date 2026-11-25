#!/usr/bin/env python3
"""
Unit and Benchmark Tests for Human Natural Language Queries & Custom Commands
Validates that casual, colloquial, and custom human inputs map accurately to structured tools.
"""

import os
import unittest
from mavrick.tests import IsolatedTestCase
from unittest.mock import patch

from mavrick.config import (
    get_custom_command,
    set_custom_command,
    delete_custom_command,
    list_custom_commands,
    load_user_config
)
from mavrick.schema import ActionType, RiskLevel, ActionRequest
from mavrick.ai import query_model, parse_model_response, build_system_prompt
from mavrick.executor import ActionExecutor

class TestHumanQueriesAndCustomCommands(IsolatedTestCase):
    def setUp(self):
        self.executor = ActionExecutor.get_instance()

    def test_default_custom_commands_fast_path(self):
        """Verifies built-in custom commands resolve immediately without AI call."""
        # 1. Exact match 'gaming mode'
        resp_game = query_model("gaming mode")
        self.assertEqual(resp_game.response_type, "action")
        self.assertIsNotNone(resp_game.action_request)
        self.assertEqual(resp_game.action_request.action_type, ActionType.SET_POWER_PROFILE)
        self.assertEqual(resp_game.action_request.params["profile"], "performance")

        # 2. Match with conversational filler words ("please switch to battery saver")
        resp_bat = query_model("please switch to battery saver")
        self.assertEqual(resp_bat.response_type, "action")
        self.assertIsNotNone(resp_bat.action_request)
        self.assertEqual(resp_bat.action_request.action_type, ActionType.SET_POWER_PROFILE)
        self.assertEqual(resp_bat.action_request.params["profile"], "battery")

        # 3. Match 'mute all'
        resp_mute = query_model("mute all")
        self.assertEqual(resp_mute.response_type, "action")
        self.assertEqual(resp_mute.action_request.action_type, ActionType.SET_VOLUME)
        self.assertTrue(resp_mute.action_request.params.get("mute"))

        # 4. Match 'free storage'
        resp_storage = query_model("free storage")
        self.assertEqual(resp_storage.response_type, "action")
        self.assertEqual(resp_storage.action_request.action_type, ActionType.INSPECT_STORAGE)

        # 5. Match 'clean junk'
        resp_clean = query_model("run clean junk")
        self.assertEqual(resp_clean.response_type, "action")
        self.assertEqual(resp_clean.action_request.action_type, ActionType.CLEAN_CACHE)

    def test_dynamic_custom_command_creation_and_execution(self):
        """Test defining a new custom shortcut dynamically and executing it."""
        trig = "chill session"
        try:
            # 1. Define custom command via ActionExecutor
            define_req = ActionRequest(
                action_type=ActionType.DEFINE_CUSTOM_COMMAND,
                params={
                    "trigger": trig,
                    "target_action": "set_power_profile",
                    "params": {"profile": "battery"},
                    "summary": "Switch to battery for chill session"
                },
                summary="Define chill session shortcut",
                risk_level=RiskLevel.REVERSIBLE
            )
            # 1. Unconfirmed returns requires_confirmation
            res_unconfirmed = self.executor.execute(define_req, confirmed=False)
            self.assertEqual(res_unconfirmed.status, "requires_confirmation")

            # 2. Confirmed executes successfully
            res_def = self.executor.execute(define_req, confirmed=True)
            self.assertEqual(res_def.status, "success")
            self.assertEqual(res_def.verification_status, "verified")

            # 3. Query model with new trigger
            resp_custom = query_model(trig)
            self.assertEqual(resp_custom.response_type, "action")
            self.assertEqual(resp_custom.action_request.action_type, ActionType.SET_POWER_PROFILE)
            self.assertEqual(resp_custom.action_request.params["profile"], "battery")
            self.assertEqual(resp_custom.action_request.summary, "Set power profile to 'battery'")

            # 4. Query with filler words
            resp_filler = query_model("can you activate chill session")
            self.assertEqual(resp_filler.response_type, "action")
            self.assertEqual(resp_filler.action_request.action_type, ActionType.SET_POWER_PROFILE)

        finally:
            # Cleanup
            delete_custom_command(trig)

    def test_delete_custom_command(self):
        """Test removing a custom command shortcut."""
        trig = "temp shortcut"
        set_custom_command(trig, "lock_session", {}, "Lock screen shortcut")
        self.assertIsNotNone(get_custom_command(trig))

        del_req = ActionRequest(
            action_type=ActionType.DELETE_CUSTOM_COMMAND,
            params={"trigger": trig},
            summary=f"Delete {trig}",
            risk_level=RiskLevel.REVERSIBLE
        )
        res_unconfirmed = self.executor.execute(del_req, confirmed=False)
        self.assertEqual(res_unconfirmed.status, "requires_confirmation")

        res_del = self.executor.execute(del_req, confirmed=True)
        self.assertEqual(res_del.status, "success")
        self.assertIsNone(get_custom_command(trig))

    def test_system_prompt_contains_training_examples(self):
        """Verify the system prompt includes all human intent training examples and active custom commands."""
        prompt = build_system_prompt()
        self.assertIn("NATURAL LANGUAGE HUMAN INTENT TRAINING EXAMPLES", prompt)
        self.assertIn("why is my laptop heating up", prompt)
        self.assertIn("what is eating my ram", prompt)
        self.assertIn("which app have most storage used", prompt)
        self.assertIn("game mode", prompt)
        self.assertIn("pause Spotify", prompt)
        self.assertIn("check wifi", prompt)
        self.assertIn("clean up my downloads folder", prompt)
        self.assertIn("Active User Custom Shortcuts", prompt)

    def test_human_query_parsing_benchmarks(self):
        """
        Benchmark test simulating realistic casual human natural language queries
        and verifying that the parsed outputs map to strictly valid ActionRequests.
        """
        benchmarks = [
            (
                '{"type": "action", "action": "inspect_processes", "params": {"sort_by": "cpu", "limit": 10}, "summary": "Checking CPU hogs"}',
                ActionType.INSPECT_PROCESSES,
                {"sort_by": "cpu", "limit": 10}
            ),
            (
                '{"type": "action", "action": "inspect_storage", "params": {"category": "apps", "limit": 10}, "summary": "Inspecting app storage"}',
                ActionType.INSPECT_STORAGE,
                {"category": "apps", "limit": 10}
            ),
            (
                '{"type": "action", "action": "clean_cache", "params": {"dry_run": true}, "summary": "Calculating reclaimable cache space"}',
                ActionType.CLEAN_CACHE,
                {"dry_run": True}
            ),
            (
                '{"type": "action", "action": "control_media", "params": {"action": "play_pause"}, "summary": "Toggling media playback"}',
                ActionType.CONTROL_MEDIA,
                {"action": "play_pause"}
            ),
            (
                '{"type": "action", "action": "get_network_status", "params": {}, "summary": "Checking network status"}',
                ActionType.GET_NETWORK_STATUS,
                {}
            ),
            (
                '{"type": "action", "action": "set_power_profile", "params": {"profile": "performance"}, "summary": "Switching to performance profile"}',
                ActionType.SET_POWER_PROFILE,
                {"profile": "performance"}
            ),
            (
                '{"type": "action", "action": "set_volume", "params": {"level": 35}, "summary": "Setting volume to 35%"}',
                ActionType.SET_VOLUME,
                {"level": 35}
            ),
            (
                '{"type": "action", "action": "organize_downloads", "params": {"dry_run": true}, "summary": "Generating downloads preview"}',
                ActionType.ORGANIZE_DOWNLOADS,
                {"dry_run": True}
            ),
            (
                '{"type": "action", "action": "lock_session", "params": {}, "summary": "Locking screen"}',
                ActionType.LOCK_SESSION,
                {}
            ),
            (
                '{"type": "action", "action": "run_in_terminal", "params": {"command": "htop"}, "summary": "Opening htop in terminal"}',
                ActionType.RUN_IN_TERMINAL,
                {"command": "htop"}
            )
        ]

        for raw_json, expected_action, expected_params in benchmarks:
            resp = parse_model_response(raw_json)
            self.assertEqual(resp.response_type, "action")
            self.assertIsNotNone(resp.action_request)
            self.assertEqual(resp.action_request.action_type, expected_action)
            for k, v in expected_params.items():
                self.assertEqual(resp.action_request.params.get(k), v)

if __name__ == "__main__":
    unittest.main()
