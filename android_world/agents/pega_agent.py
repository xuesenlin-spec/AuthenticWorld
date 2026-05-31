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

"""PegaAgent: Long-horizon GUI agent with Fast-Path and reflection capabilities."""

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
# UI Lifetime Detection (Fast-Path)
# ---------------------------------------------------------------------------

SYSTEM_PACKAGES = {
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.android.settings",
    "com.android.systemui",
    "com.android.server.telecom",
    "com.android.permissioncontroller",
}

TRANSIENT_KEYWORDS = [
    r"set.*default.*app",
    r"set.*default.*SMS",
    r"CHANGE",
    r"ALLOW",
    r"DENY",
    r"permission",
    r"isn't responding",
    r"Close app",
    r"Wait",
    r"No internet",
    r"Network error",
]


def detect_transient_ui(ui_elements) -> bool:
    """Detect if current UI contains a transient (ephemeral) dialog."""
    for e in ui_elements:
        if e.package and e.package in SYSTEM_PACKAGES:
            return True
        if e.text and any(
            re.search(kw, e.text, re.IGNORECASE) for kw in TRANSIENT_KEYWORDS
        ):
            return True
    return False


def get_fast_path_action(ui_elements) -> dict | None:
    """Return a fast-path action to handle a transient UI dialog."""
    priority_buttons = {"CHANGE", "ALLOW", "OK", "SET DEFAULT", "CONFIRM"}
    dismiss_buttons = {"CANCEL", "DENY", "DISMISS", "NOT NOW", "LATER"}

    for text_set in [priority_buttons, dismiss_buttons]:
        for e in ui_elements:
            if not e.clickable:
                continue
            if e.text and e.text.upper() in text_set:
                return {"action_type": "click", "index": e.index}

    # Fallback: go back
    return {"action_type": "navigate_back"}


# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------

class LoopDetector:
    """Detect when the agent is stuck in a repetitive loop."""

    def __init__(self, window: int = 3):
        self.window = window
        self.history = []  # [(action_type, element_text_or_target)]

    def record(self, action_type: str, target: str = "") -> None:
        self.history.append((action_type, target))

    def is_stuck(self) -> bool:
        if len(self.history) < self.window * 2:
            return False
        recent = self.history[-self.window:]
        older = self.history[-self.window * 2 : -self.window]
        # Check if recent actions are identical to older ones
        if not recent or not older:
            return False
        return all(r == o for r, o in zip(recent, older))

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
# Reflection Controller
# ---------------------------------------------------------------------------

REFLECTION_PROMPT_TEMPLATE = (
    "You are an agent stuck in a loop. Analyze what went wrong and propose a "
    "new strategy.\n\n"
    "**Original Goal**: {goal}\n\n"
    "**Recent History (last {N} steps)**:\n{history}\n\n"
    "**Problem Detected**: {problem}\n\n"
    "Analyze:\n"
    "1. What is the root cause of the failure? (not the surface symptom)\n"
    "2. What assumption was wrong in the previous approach?\n"
    "3. What alternative strategies could work?\n"
    "4. Pick ONE new strategy and explain the first action to take.\n\n"
    "Respond with:\n"
    "Reflection: [your analysis]\n"
    "New Strategy: [the new approach]\n"
    'First Action: {{"action_type": "...", ...}}\n'
)


def _build_reflection_prompt(
    goal: str, history: list[str], problem: str, window: int
) -> str:
    history_str = "\n".join(history) if history else "No history available."
    return REFLECTION_PROMPT_TEMPLATE.format(
        goal=goal,
        N=window * 2,
        history=history_str,
        problem=problem,
    )


def _parse_reflection_result(text: str) -> tuple[str, str | None]:
    """Parse reflection output to extract reason and action JSON."""
    action = None
    for line in text.split("\n"):
        if line.strip().startswith("First Action:"):
            action_part = line.split("First Action:", 1)[1].strip()
            # Extract JSON from the line
            start = action_part.find("{")
            end = action_part.rfind("}") + 1
            if start >= 0 and end > start:
                action = action_part[start:end]
    # Extract reason from Reflection line
    reason = "Reflection triggered due to repetitive loop."
    for line in text.split("\n"):
        if line.strip().startswith("Reflection:"):
            reason = line.split("Reflection:", 1)[1].strip()[:200]
            break
    return reason, action


# ---------------------------------------------------------------------------
# PegaAgent
# ---------------------------------------------------------------------------

class PegaAgent(t3a.T3A):
    """PegaAgent: T3A with Fast-Path for transient UI and loop reflection."""

    def __init__(
        self,
        env: interface.AsyncEnv,
        llm: infer.LlmWrapper,
        name: str = "PegaAgent",
    ):
        super().__init__(env, llm, name)
        self.loop_detector = LoopDetector(window=3)
        self.reflection_count = 0
        self.max_reflections = 3

    def reset(self, go_home_on_reset: bool = False):
        super().reset(go_home_on_reset)
        self.loop_detector = LoopDetector(window=3)
        self.reflection_count = 0

    def _execute_action_and_record(
        self, converted_action, ui_elements, step_data, logical_screen_size
    ) -> tuple[bool, str]:
        """Execute action, record for loop detection. Returns (success, target_desc)."""
        target_desc = ""
        if converted_action.index is not None and converted_action.index < len(ui_elements):
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

        # --- Fast-Path: detect transient UI and respond immediately ---
        if detect_transient_ui(ui_elements):
            print("[FAST-PATH] Transient UI detected, bypassing LLM.")
            fp_action = get_fast_path_action(ui_elements)
            if fp_action:
                print(f"[FAST-PATH] Executing: {fp_action}")
                converted_action = json_action.JSONAction(**fp_action)

                ok, _ = self._execute_action_and_record(
                    converted_action, ui_elements, step_data, logical_screen_size
                )
                if not ok:
                    step_data["summary"] = "Fast-Path action failed."
                    self.history.append(step_data)
                    return base_agent.AgentInteractionResult(False, step_data)

                # After fast-path, get new state and continue to normal flow
                state = self.get_post_transition_state()
                ui_elements = state.ui_elements
                before_element_list = t3a._generate_ui_elements_description_list_full(
                    ui_elements, logical_screen_size
                )
                step_data["after_screenshot"] = state.pixels.copy()
                step_data["after_element_list"] = ui_elements

        # --- Normal LLM decision flow ---
        action_prompt = t3a._action_selection_prompt(
            goal,
            [
                "Step " + str(i + 1) + ": " + step_info["summary"]
                for i, step_info in enumerate(self.history)
            ],
            before_element_list,
            self.additional_guidelines,
        )
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

        print("Action: " + action)
        print("Reason: " + reason)

        try:
            converted_action = json_action.JSONAction(
                **agent_utils.extract_json(action),
            )
        except Exception as e:
            print("Failed to convert the output to a valid action.")
            print(str(e))
            step_data["summary"] = (
                "Can not parse the output to a valid action. Please make sure to pick"
                " the action from the list with the correct json format!"
            )
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

        # --- Loop Detection + Reflection ---
        if self.loop_detector.is_stuck():
            problem = self.loop_detector.get_problem_description()
            print(f"\n[REFLECTION] Loop detected! Problem: {problem}")

            if self.reflection_count < self.max_reflections:
                # Build reflection prompt from recent history
                recent_history = [
                    f"Step {step_num - i}: {s.get('summary', 'N/A')}"
                    for i, s in enumerate(reversed(self.history[-self.loop_detector.window * 2:]))
                    if s.get("summary")
                ]
                refl_prompt = _build_reflection_prompt(
                    goal, recent_history, problem, self.loop_detector.window
                )

                _t = time.time()
                refl_output, _, refl_raw = self.llm.predict(refl_prompt)
                print(f"[TIMING] LLM reflection: {time.time()-_t:.2f}s")

                refl_reason, refl_action = _parse_reflection_result(refl_output)
                print(f"[REFLECTION] Reason: {refl_reason}")
                print(f"[REFLECTION] New Action: {refl_action}")

                if refl_action:
                    try:
                        refl_converted = json_action.JSONAction(
                            **agent_utils.extract_json(refl_action)
                        )
                        # Execute reflection action
                        ok2, _ = self._execute_action_and_record(
                            refl_converted, ui_elements, step_data, logical_screen_size
                        )
                        if ok2:
                            self.reflection_count += 1
                            print(
                                f"[REFLECTION] Reflection action executed successfully. "
                                f"(reflection #{self.reflection_count})"
                            )
                    except Exception as e:
                        print(f"[REFLECTION] Failed to execute reflection action: {e}")

        state = self.get_post_transition_state()
        print(
            f"[TIMING] get_post_transition_state (after action): {time.time()-_t:.2f}s"
        )
        ui_elements = state.ui_elements

        after_element_list = t3a._generate_ui_elements_description_list_full(
            ui_elements, self.env.logical_screen_size
        )

        step_data["after_screenshot"] = state.pixels.copy()
        step_data["after_element_list"] = ui_elements

        summary = f"Action: {action}. Reason: {reason}"
        step_data["summary_prompt"] = None
        step_data["summary"] = summary
        step_data["summary_raw_response"] = None
        print("Summary: " + summary)

        print(f"[TIMING] STEP {step_num} TOTAL: {time.time()-_step_start:.2f}s")
        print("---")

        self.history.append(step_data)

        return base_agent.AgentInteractionResult(False, step_data)
