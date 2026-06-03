# Copyright 2026 The android_world Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PegaAgent: Long-horizon GUI agent with Fast-Path and loop reflection."""

import re
import time
from typing import Any

from android_world.agents import agent_utils
from android_world.agents import base_agent
from android_world.agents import infer
from android_world.agents import m3a_utils
from android_world.agents import t3a
from android_world.env import adb_utils
from android_world.env import interface
from android_world.env import json_action
from android_world.env import representation_utils

# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------

LOOP_DETECTION_THRESHOLD = 3
MAX_REFLECTIONS = 3


class LoopDetector:
    """Detect when the agent is stuck in a repetitive loop."""

    def __init__(self, window: int = LOOP_DETECTION_THRESHOLD):
        self.window = window
        self.history = []  # [(action_type, element_text_or_target)]

    def record(self, action_type: str, target: str = "") -> None:
        self.history.append((action_type, target))

    def is_stuck(self) -> bool:
        if len(self.history) < self.window:
            return False
        # Trigger if the last N actions are all identical
        last = self.history[-1]
        return all(h == last for h in self.history[-self.window:])

    def get_problem_description(self) -> str:
        if not self.history:
            return ""
        action_type, target = self.history[-1]
        return (
            f"The action '{action_type}' on '{target}' has been repeated "
            f"{self.window} times without progress. "
            "The agent appears to be stuck in a loop."
        )


# ---------------------------------------------------------------------------
# Fast-Path: LLM-controlled tool for handling transient system dialogs
# ---------------------------------------------------------------------------

SYSTEM_DIALOG_KEYWORDS = [
    r"set.*default.*app",
    r"set.*default.*SMS",
    r"ALLOW",
    r"DENY",
    r"permission",
    r"isn't responding",
    r"Close app",
    r"No internet",
    r"Network error",
]


def _find_dialog_button(ui_elements, button_set: set) -> int | None:
    """Find a button matching one of the given texts."""
    for i, e in enumerate(ui_elements):
        if not getattr(e, "is_clickable", None):
            continue
        if e.text and e.text.upper() in button_set:
            return i
    return None


def execute_fast_path(ui_elements, env, button_text: str = None) -> tuple[bool, str]:
    """Execute a one-shot Fast-Path to handle a system dialog.

    Args:
        ui_elements: Current UI elements.
        env: Environment for executing action.
        button_text: Specific button text to click (LLM-specified).
            If None, falls back to auto-detect (CHANGE, ALLOW, etc.).

    Returns (success, button_clicked_or_none).
    """
    btn_index = None
    if button_text:
        btn_index = _find_dialog_button(ui_elements, {button_text.upper()})
    else:
        # Auto-detect: priority first, then dismiss
        priority_buttons = {
            "CHANGE", "ALLOW", "OK", "SET DEFAULT", "SET AS DEFAULT", "CONFIRM",
        }
        dismiss_buttons = {"CANCEL", "DENY", "DISMISS", "NOT NOW", "LATER"}
        btn_index = _find_dialog_button(ui_elements, priority_buttons)
        if btn_index is None:
            btn_index = _find_dialog_button(ui_elements, dismiss_buttons)

    if btn_index is None:
        return False, "no_actionable_button"

    elem = ui_elements[btn_index]
    clicked_text = elem.text or f"element_{btn_index}"

    action = json_action.JSONAction(action_type="click", index=btn_index)
    try:
        env.execute_action(action)
        return True, clicked_text
    except Exception as e:
        print(f"[FAST-PATH] Failed: {e}")
        return False, clicked_text


def get_dialog_context(ui_elements) -> str:
    """Extract dialog text for LLM context."""
    texts = []
    for e in ui_elements:
        if e.text:
            texts.append(e.text)
    return " | ".join(texts)


def has_dialog(ui_elements) -> bool:
    """Check if current screen has a system dialog."""
    for e in ui_elements:
        if e.text and any(
            re.search(kw, e.text, re.IGNORECASE) for kw in SYSTEM_DIALOG_KEYWORDS
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Loop notification prompt
# ---------------------------------------------------------------------------

LOOP_NOTIFY_PROMPT = (
    "\n\n**WARNING: Loop Detected**\n"
    "{problem}\n"
    "Analyze the situation and choose a different strategy.\n"
    "You can use the fast_path tool to handle system dialogs:\n"
    '- `{{"action_type": "fast_path", "mode": "immediate", "button_text": "CHANGE"}}` '
    '→ Check for dialog NOW and click button immediately.\n'
    '- `{{"action_type": "fast_path", "mode": "next_step", "button_text": "CHANGE"}}` '
    '→ Set a deferred handler: after your next action executes, check if a dialog '
    'appears and click the button. Use this when the dialog only appears AFTER your '
    'action (e.g., clicking Send triggers the dialog). fast_path is one-shot: '
    'after it triggers once, it is consumed.\n'
)


# ---------------------------------------------------------------------------
# PegaAgent
# ---------------------------------------------------------------------------

class PegaAgent(t3a.T3A):
    """PegaAgent: T3A with LLM-controlled Fast-Path and loop notification."""

    def __init__(
        self,
        env: interface.AsyncEnv,
        llm: infer.LlmWrapper,
        name: str = "PegaAgent",
    ):
        super().__init__(env, llm, name)
        self.loop_detector = LoopDetector(window=LOOP_DETECTION_THRESHOLD)
        self.reflection_count = 0
        self.max_reflections = MAX_REFLECTIONS
        # Deferred fast_path: set by LLM, checked after action execution
        self.deferred_fast_path = None  # {"button_text": str} or None

    def reset(self, go_home_on_reset: bool = False):
        super().reset(go_home_on_reset)
        self.loop_detector = LoopDetector(window=LOOP_DETECTION_THRESHOLD)
        self.reflection_count = 0
        self.deferred_fast_path = None

    def _execute_action_and_record(
        self, converted_action, ui_elements, step_data, logical_screen_size
    ) -> tuple[bool, str]:
        """Execute action, record for loop detection. Returns (success, target_desc)."""
        target_desc = ""
        if (converted_action.index is not None
                and converted_action.index < len(ui_elements)):
            elem = ui_elements[converted_action.index]
            target_desc = elem.text or f"element_{converted_action.index}"

        self.loop_detector.record(converted_action.action_type, target_desc)

        try:
            _t = time.time()
            self.env.execute_action(converted_action)
            print(f"[TIMING] execute_action: {time.time()-_t:.2f}s")
            return True, target_desc
        except Exception as e:
            print(
                "Some error happened executing the action ",
                converted_action.action_type,
            )
            print(str(e))
            step_data["summary"] = (
                "Some error happened executing the action "
                + converted_action.action_type
            )
            return False, target_desc

    def _handle_fast_path_immediate(self, ui_elements, step_data, button_text: str = None):
        """Handle fast_path mode=immediate: check dialog now and click button."""
        if has_dialog(ui_elements):
            dialog_text = get_dialog_context(ui_elements)
            print(f"[FAST-PATH immediate] Dialog: {dialog_text}")
            ok, clicked_text = execute_fast_path(ui_elements, self.env, button_text)
            if ok:
                summary = (
                    f"[FAST-PATH immediate] Dialog handled. "
                    f"Clicked '{clicked_text}'."
                )
            else:
                summary = (
                    f"[FAST-PATH immediate] No button '{button_text}' found on dialog. "
                    f"Dialog: {dialog_text}"
                )
            return True, summary
        else:
            print("[FAST-PATH immediate] No dialog present.")
            return False, "[FAST-PATH immediate] No dialog present, action not taken."

    def _handle_fast_path_next_step(self, ui_elements, button_text: str):
        """Set deferred fast_path: will trigger after next action executes."""
        self.deferred_fast_path = {"button_text": button_text}
        print(f"[FAST-PATH next_step] Deferred: will click '{button_text}' if dialog appears after next action.")

    def _check_and_execute_deferred_fast_path(self, ui_elements, step_data, logical_screen_size):
        """After action execution, check deferred fast_path and handle if dialog exists."""
        if self.deferred_fast_path is None:
            return None

        button_text = self.deferred_fast_path["button_text"]
        self.deferred_fast_path = None  # One-shot: clear after check

        if has_dialog(ui_elements):
            dialog_text = get_dialog_context(ui_elements)
            print(f"[FAST-PATH deferred] Dialog appeared: {dialog_text}")
            ok, clicked_text = execute_fast_path(ui_elements, self.env, button_text)
            if ok:
                summary = (
                    f"[FAST-PATH deferred] Dialog handled automatically. "
                    f"Clicked '{clicked_text}' (button='{button_text}')."
                )
            else:
                summary = (
                    f"[FAST-PATH deferred] Dialog appeared but button '{button_text}' "
                    f"not found. Dialog: {dialog_text}"
                )
            return summary
        else:
            print(f"[FAST-PATH deferred] No dialog after action. Deferred fast_path consumed.")
            return None

    def step(self, goal: str) -> base_agent.AgentInteractionResult:
        step_data = {
            "before_screenshot": None,
            "after_screenshot": None,
            "before_element_list": None,
            "after_element_list": None,
            "action_prompt": None,
            "action_output": None,
            "action_raw_response": None,
            "summary_prompt": None,
            "summary": None,
            "summary_raw_response": None,
        }
        step_num = len(self.history) + 1
        _step_start = time.time()
        if self._task_name:
            print(
                f"----------step {step_num}/{self._max_steps or '?'} "
                f"(Task: {self._task_name} [{self._task_count}/{self._total_tasks}])"
            )
        else:
            print(f"----------step {step_num}")

        _t = time.time()
        state = self.get_post_transition_state()
        print(
            f"[TIMING] get_post_transition_state (before LLM): {time.time()-_t:.2f}s"
        )
        logical_screen_size = self.env.logical_screen_size

        ui_elements = state.ui_elements
        before_element_list = t3a._generate_ui_elements_description_list_full(
            ui_elements, logical_screen_size
        )
        step_data["before_screenshot"] = state.pixels.copy()
        step_data["before_element_list"] = ui_elements

        # --- Check for loop and add notification to prompt ---
        history_summaries = [
            "Step " + str(i + 1) + ": " + step_info["summary"]
            for i, step_info in enumerate(self.history)
        ]
        loop_notify = ""
        if self.loop_detector.is_stuck():
            problem = self.loop_detector.get_problem_description()
            print(f"\n[LOOP DETECTED] {problem}")
            loop_notify = LOOP_NOTIFY_PROMPT.format(problem=problem)

        # Build action prompt
        action_prompt = t3a._action_selection_prompt(
            goal,
            history_summaries,
            before_element_list,
            self.additional_guidelines,
        ) + loop_notify

        step_data["action_prompt"] = action_prompt
        _t = time.time()
        action_output, is_safe, raw_response = self.llm.predict(action_prompt)
        print(f"[TIMING] LLM action selection: {time.time()-_t:.2f}s")

        if is_safe is False:
            action_output = (
                f"Reason: {m3a_utils.TRIGGER_SAFETY_CLASSIFIER}\n"
                f'Action: {{"action_type": "status", "goal_status": "infeasible"}}'
            )

        if not raw_response:
            raise RuntimeError("Error calling LLM in action selection phase.")

        step_data["action_output"] = action_output
        step_data["action_raw_response"] = raw_response

        reason, action = m3a_utils.parse_reason_action_output(action_output)

        if (not reason) or (not action):
            print("Action prompt output is not in the correct format.")
            step_data["summary"] = (
                "Output for action selection is not in the correct format, so no"
                " action is performed."
            )
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(False, step_data)

        print("Reason: " + reason)
        print("Action: " + action)

        # --- Parse action and handle fast_path BEFORE creating JSONAction ---
        try:
            action_dict = agent_utils.extract_json(action)
        except Exception as e:
            print("Failed to convert the output to a valid action.")
            print(str(e))
            step_data["summary"] = (
                "Can not parse the output to a valid action. Please make sure to pick"
                " the action from the list with the correct JSON format!"
            )
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(False, step_data)

        # Handle fast_path action type
        if action_dict.get("action_type") == "fast_path":
            mode = action_dict.get("mode", "immediate")
            button_text = action_dict.get("button_text", None)

            if mode == "next_step":
                self._handle_fast_path_next_step(ui_elements, button_text)
                # Don't execute a normal action, just record and continue
                step_data["summary"] = f"[FAST-PATH next_step] Deferred handler set for '{button_text}'."
                self.history.append(step_data)
                return base_agent.AgentInteractionResult(False, step_data)

            elif mode == "immediate":
                handled, summary = self._handle_fast_path_immediate(
                    ui_elements, step_data, button_text,
                )
                step_data["summary"] = summary
                self.history.append(step_data)
                if not handled:
                    return base_agent.AgentInteractionResult(False, step_data)
                # Get new state after fast_path
                state = self.get_post_transition_state()
                ui_elements = state.ui_elements
                before_element_list = t3a._generate_ui_elements_description_list_full(
                    ui_elements, logical_screen_size,
                )
                step_data["before_screenshot"] = state.pixels.copy()
                step_data["before_element_list"] = ui_elements
                # Re-prompt LLM with new state
                history_summaries = [
                    "Step " + str(i + 1) + ": " + step_info["summary"]
                    for i, step_info in enumerate(self.history)
                ]
                action_prompt = t3a._action_selection_prompt(
                    goal, history_summaries, before_element_list,
                    self.additional_guidelines,
                )
                step_data["action_prompt"] = action_prompt
                _t = time.time()
                action_output, is_safe, raw_response = self.llm.predict(action_prompt)
                print(f"[TIMING] LLM (after fast_path): {time.time()-_t:.2f}s")
                if not raw_response:
                    raise RuntimeError("Error calling LLM after fast_path.")
                reason, action = m3a_utils.parse_reason_action_output(action_output)
                if (not reason) or (not action):
                    step_data["summary"] = "LLM format error after fast_path."
                    self.history.append(step_data)
                    return base_agent.AgentInteractionResult(False, step_data)
                print("Reason (after fast_path): " + reason)
                print("Action (after fast_path): " + action)
                try:
                    action_dict = agent_utils.extract_json(action)
                except Exception:
                    self.history.append(step_data)
                    return base_agent.AgentInteractionResult(False, step_data)

        # --- Execute normal action ---
        try:
            converted_action = json_action.JSONAction(**action_dict)
        except Exception as e:
            print("Failed to create JSONAction: " + str(e))
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(False, step_data)

        if converted_action.action_type in ["click", "long-press", "input-text"]:
            if converted_action.index is not None and converted_action.index >= len(
                ui_elements
            ):
                print("Index out of range.")
                step_data["summary"] = (
                    "The parameter index is out of range. Remember the index must be in"
                    " the UI element list!"
                )
                self.history.append(step_data)
                return base_agent.AgentInteractionResult(False, step_data)
            else:
                m3a_utils.add_ui_element_mark(
                    step_data["before_screenshot"],
                    ui_elements[converted_action.index],
                    converted_action.index,
                    logical_screen_size,
                    adb_utils.get_physical_frame_boundary(self.env.controller),
                    adb_utils.get_orientation(self.env.controller),
                )

        if converted_action.action_type == "status":
            if converted_action.goal_status == "infeasible":
                print("Agent stopped since it thinks mission impossible.")
            step_data["summary"] = "Agent thinks the request has been completed."
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(True, step_data)

        if converted_action.action_type == "answer":
            print("Agent answered with: " + converted_action.text)

        ok, target_desc = self._execute_action_and_record(
            converted_action, ui_elements, step_data, logical_screen_size
        )
        if not ok:
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(False, step_data)

        # --- Check deferred fast_path: dialog appeared after action? ---
        state = self.get_post_transition_state()
        fp_summary = self._check_and_execute_deferred_fast_path(
            state.ui_elements, step_data, logical_screen_size,
        )
        if fp_summary:
            print(f"[FAST-PATH deferred] {fp_summary}")
            # Re-get state after fast_path
            state = self.get_post_transition_state()

        ui_elements = state.ui_elements

        after_element_list = t3a._generate_ui_elements_description_list_full(
            ui_elements, self.env.logical_screen_size
        )

        step_data["after_screenshot"] = state.pixels.copy()
        step_data["after_element_list"] = ui_elements

        summary = f"Action: {action}. Reason: {reason}"
        if fp_summary:
            summary += f"\n{fp_summary}"
        step_data["summary_prompt"] = None
        step_data["summary"] = summary
        step_data["summary_raw_response"] = None
        print("Summary: " + summary)

        print(f"[TIMING] STEP {step_num} TOTAL: {time.time()-_step_start:.2f}s")
        print("---")

        self.history.append(step_data)

        return base_agent.AgentInteractionResult(False, step_data)
