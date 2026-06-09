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
from android_world.agents import m3a
from android_world.agents import m3a_utils
from android_world.env import adb_utils
from android_world.env import interface
from android_world.env import json_action
from android_world.env import representation_utils
from android_world.sekr.engine import SEKREngine

# ---------------------------------------------------------------------------
# Loop Detection
# ---------------------------------------------------------------------------

LOOP_DETECTION_THRESHOLD = 6
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
# Goal-Aware Working Memory
# ---------------------------------------------------------------------------

GOAL_AWARE_MEMORY_PROMPT = (
    "\n\n--- Goal-Aware Memory ---\n"
    "Your overall goal is: {goal}\n"
    "Look at the current screen carefully. Based on your goal, is there any "
    "specific information here (numbers, names, dates, file contents, rules, "
    "or decisions) that you will need LATER to complete the goal?\n"
    "If yes, record it using: [REMEMBER: key=value, key2=value2]\n"
    "If nothing needs to be remembered, you can skip this.\n"
)

WORKING_MEMORY_CONTEXT = (
    "\n\n--- Your Working Memory (facts recorded from previous steps) ---\n"
    "{memory_text}\n"
    "Use these facts when making decisions.\n"
)

FEYNMAN_VERIFY_PROMPT = (
    "\n\n--- Feynman Self-Check ---\n"
    "Before finalizing your action, explain simply (in one sentence):\n"
    "1. What exactly is at the index you selected? (e.g., 'a Button labeled "
    "'Submit'', 'a text field showing the number 5')\n"
    "2. Does interacting with this element achieve your goal? Why?\n"
    "3. If you are unsure or it doesn't match, what should you click instead?\n"
    "If the selected index is correct, proceed with your Action as usual.\n"
    "If NOT, output 'Corrected Action' with the right JSON after your "
    "explanation.\n"
)

FEYNMAN_VERIFY_WITH_SEKR_PROMPT = (
    "\n\n--- Feynman Self-Check ---\n"
    "Before finalizing your action, explain simply (in one sentence):\n"
    "1. What exactly is at the index you selected? (e.g., 'a Button labeled "
    "'Submit'', 'a text field showing the number 5')\n"
    "2. Does interacting with this element achieve your goal? Why?\n"
    "3. If you are unsure or it doesn't match, what should you click instead?\n"
    "4. **SEKR Compliance Check**: Review the SEKR Knowledge Base rules above. "
    "Does your current action comply with each applicable rule? For every rule, "
    "confirm explicitly: '[OK] Rule: <rule summary> - Complied' or "
    "'[FAIL] Rule: <rule summary> - NOT followed, need to adjust action.'\n"
    "   - If any rule shows [FAIL], you MUST adjust your action before proceeding.\n"
    "If the selected index is correct AND all SEKR rules are complied, "
    "proceed with your Action as usual.\n"
    "If NOT, output 'Corrected Action' with the right JSON after your "
    "explanation.\n"
)


class WorkingMemory:
    """Stores facts extracted by the LLM during task execution."""

    def __init__(self):
        self.facts = []  # list of (step_num, dict) tuples

    def add(self, step_num: int, fact_dict: dict[str, str]) -> None:
        self.facts.append((step_num, fact_dict))

    def get_text(self) -> str:
        if not self.facts:
            return "(empty)"
        lines = []
        for step, facts in self.facts:
            for k, v in facts.items():
                lines.append(f"  [Step {step}] {k} = {v}")
        return "\n".join(lines)

    def clear(self) -> None:
        self.facts = []


# ---------------------------------------------------------------------------
# PegaAgent
# ---------------------------------------------------------------------------

class PegaAgent(m3a.M3A):
    """PegaAgent: M3A with LLM-controlled Fast-Path and loop notification."""

    def __init__(
        self,
        env: interface.AsyncEnv,
        llm: infer.MultimodalLlmWrapper,
        name: str = "PegaAgent",
    ):
        super().__init__(env, llm, name)
        self.loop_detector = LoopDetector(window=LOOP_DETECTION_THRESHOLD)
        self.reflection_count = 0
        self.max_reflections = MAX_REFLECTIONS
        # Deferred fast_path: set by LLM, checked after action execution
        self.deferred_fast_path = None  # {"button_text": str} or None
        # Goal-aware working memory
        self.working_memory = WorkingMemory()
        # SEKR Engine (Knowledge Infusion)
        self.sekr_engine = SEKREngine()

    def reset(self, go_home_on_reset: bool = False):
        super().reset(go_home_on_reset)
        self.loop_detector = LoopDetector(window=LOOP_DETECTION_THRESHOLD)
        self.reflection_count = 0
        self.deferred_fast_path = None
        self.working_memory = WorkingMemory()

    def _format_working_memory_context(self) -> str:
        """Format working memory for injection into the action prompt."""
        memory_text = self.working_memory.get_text()
        return WORKING_MEMORY_CONTEXT.format(memory_text=memory_text)

    def _extract_and_save_remembered_facts(self, step_num: int, text: str) -> None:
        """Parse [REMEMBER: key=value, ...] from LLM output and save to memory."""
        pattern = r'\[REMEMBER:\s*(.*?)\]'
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            fact_dict = {}
            # Split by comma, parse key=value pairs
            for pair in match.split(','):
                pair = pair.strip()
                if '=' in pair:
                    key, _, value = pair.partition('=')
                    fact_dict[key.strip()] = value.strip()
            if fact_dict:
                self.working_memory.add(step_num, fact_dict)
                for k, v in fact_dict.items():
                    print(f"[MEMORY] Step {step_num}: {k} = {v}")

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
            "raw_screenshot": None,
            "before_screenshot_with_som": None,
            "after_screenshot_with_som": None,
            "before_ui_elements": [],
            "action_prompt": None,
            "action_output": None,
            "action_output_json": None,
            "action_reason": None,
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

        state = self.get_post_transition_state()
        logical_screen_size = self.env.logical_screen_size
        orientation = self.env.orientation
        physical_frame_boundary = self.env.physical_frame_boundary

        before_ui_elements = state.ui_elements
        step_data["before_ui_elements"] = before_ui_elements
        before_ui_elements_list = m3a._generate_ui_elements_description_list(
            before_ui_elements, logical_screen_size
        )
        step_data["raw_screenshot"] = state.pixels.copy()
        before_screenshot = state.pixels.copy()
        for index, ui_element in enumerate(before_ui_elements):
            if m3a_utils.validate_ui_element(ui_element, logical_screen_size):
                m3a_utils.add_ui_element_mark(
                    before_screenshot,
                    ui_element,
                    index,
                    logical_screen_size,
                    physical_frame_boundary,
                    orientation,
                )
        step_data["before_screenshot_with_som"] = before_screenshot.copy()

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

        # Build action prompt (M3A style)
        action_prompt = m3a._action_selection_prompt(
            goal,
            history_summaries,
            before_ui_elements_list,
            self.additional_guidelines,
        ) + loop_notify

        # Inject step budget awareness
        max_steps = self._max_steps or 12
        remaining = max_steps - step_num
        action_prompt += (
            f"\n\n--- Step Budget ---\n"
            f"You are at Step {step_num} of {max_steps} total steps allowed. "
            f"{remaining} step(s) remaining.\n"
            "Plan accordingly: if only 1 step remains, use it to complete the task "
            "(status:complete) rather than starting a new multi-step operation.\n"
        )

        # Inject working memory context (facts from previous steps)
        action_prompt += self._format_working_memory_context()

        # Inject SEKR knowledge (Knowledge-Infused Reasoning)
        recent_history = " ".join(history_summaries[-3:]) if history_summaries else ""
        sekr_guidance, sekr_entries = self.sekr_engine.retrieve_with_raw_text(recent_history, goal)
        action_prompt += sekr_guidance

        # Inject SEKR global rules (always injected as system-level constraints)
        if self.sekr_engine.global_rules:
            action_prompt += "\n\n--- SEKR Global Rules (MUST ALWAYS FOLLOW) ---\n"
            action_prompt += self.sekr_engine.global_rules

        # Inject goal-aware memory instruction (prompt to record new facts)
        action_prompt += GOAL_AWARE_MEMORY_PROMPT.format(goal=goal)

        # Inject Feynman self-verification instruction (with SEKR compliance if applicable)
        if sekr_entries:
            action_prompt += FEYNMAN_VERIFY_WITH_SEKR_PROMPT
            print(f"[SEKR] {len(sekr_entries)} rule(s) retrieved — SEKR compliance check enabled")
        else:
            action_prompt += FEYNMAN_VERIFY_PROMPT

        step_data["action_prompt"] = action_prompt
        _t = time.time()
        action_output, is_safe, raw_response = self.llm.predict_mm(
            action_prompt,
            [
                step_data["raw_screenshot"],
                before_screenshot,
            ],
        )
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

        # Extract and save remembered facts from LLM output
        self._extract_and_save_remembered_facts(step_num, action_output)

        reason, action = m3a_utils.parse_reason_action_output(action_output)

        # --- Feynman self-check: look for "Corrected Action" in output ---
        corrected_action_match = re.search(
            r'Corrected Action:\s*(\{.*\})', action_output, re.DOTALL
        )
        if corrected_action_match:
            corrected = corrected_action_match.group(1).strip()
            print(f"[FEYNMAN CORRECTION] Found corrected action: {corrected}")
            action = corrected
        else:
            # Feynman check passed without correction
            print("[FEYNMAN CHECK] Passed (no correction needed).")

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
        step_data["action_reason"] = reason

        # --- Check for chained actions (Action 1 + Action 2) ---
        action2_match = re.search(
            r'Action\s*2:\s*(\{.*\})', action_output, re.DOTALL
        )
        second_action_json = action2_match.group(1).strip() if action2_match else None
        if second_action_json:
            print(f"[CHAIN ACTION] Second action detected: {second_action_json}")
            step_data["chain_action_2"] = second_action_json

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
                self._handle_fast_path_next_step(before_ui_elements, button_text)
                # Don't execute a normal action, just record and continue
                step_data["summary"] = f"[FAST-PATH next_step] Deferred handler set for '{button_text}'."
                self.history.append(step_data)
                return base_agent.AgentInteractionResult(False, step_data)

            elif mode == "immediate":
                handled, summary = self._handle_fast_path_immediate(
                    before_ui_elements, step_data, button_text,
                )
                step_data["summary"] = summary
                self.history.append(step_data)
                if not handled:
                    return base_agent.AgentInteractionResult(False, step_data)
                # Get new state after fast_path
                state = self.get_post_transition_state()
                before_ui_elements = state.ui_elements
                before_ui_elements_list = m3a._generate_ui_elements_description_list(
                    before_ui_elements, logical_screen_size,
                )
                step_data["raw_screenshot"] = state.pixels.copy()
                before_screenshot = state.pixels.copy()
                for index, ui_element in enumerate(before_ui_elements):
                    if m3a_utils.validate_ui_element(ui_element, logical_screen_size):
                        m3a_utils.add_ui_element_mark(
                            before_screenshot,
                            ui_element,
                            index,
                            logical_screen_size,
                            physical_frame_boundary,
                            orientation,
                        )
                step_data["before_screenshot_with_som"] = before_screenshot.copy()
                step_data["before_ui_elements"] = before_ui_elements
                # Re-prompt LLM with new state
                history_summaries = [
                    "Step " + str(i + 1) + ": " + step_info["summary"]
                    for i, step_info in enumerate(self.history)
                ]
                action_prompt = m3a._action_selection_prompt(
                    goal, history_summaries, before_ui_elements_list,
                    self.additional_guidelines,
                )
                # Inject working memory into fast_path re-prompt too
                action_prompt += self._format_working_memory_context()
                # Re-inject SEKR knowledge (was missing in fast-path re-prompt)
                sekr_guidance, sekr_entries = self.sekr_engine.retrieve_with_raw_text(
                    " ".join(history_summaries[-3:]), goal
                )
                action_prompt += sekr_guidance
                # Re-inject global rules in fast-path re-prompt
                if self.sekr_engine.global_rules:
                    action_prompt += "\n\n--- SEKR Global Rules (MUST ALWAYS FOLLOW) ---\n"
                    action_prompt += self.sekr_engine.global_rules
                action_prompt += GOAL_AWARE_MEMORY_PROMPT.format(goal=goal)
                # Use SEKR-aware Feynman prompt if rules are active
                if sekr_entries:
                    action_prompt += FEYNMAN_VERIFY_WITH_SEKR_PROMPT
                else:
                    action_prompt += FEYNMAN_VERIFY_PROMPT
                step_data["action_prompt"] = action_prompt
                _t = time.time()
                action_output, is_safe, raw_response = self.llm.predict_mm(
                    action_prompt,
                    [
                        step_data["raw_screenshot"],
                        before_screenshot,
                    ],
                )
                print(f"[TIMING] LLM (after fast_path): {time.time()-_t:.2f}s")
                if not raw_response:
                    raise RuntimeError("Error calling LLM after fast_path.")
                # Extract remembered facts from fast_path re-prompt output too
                self._extract_and_save_remembered_facts(step_num, action_output)
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
            step_data["action_output_json"] = converted_action
        except Exception as e:
            print("Failed to create JSONAction: " + str(e))
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(False, step_data)

        if converted_action.action_type in ["click", "long_press", "input_text", "scroll"]:
            if converted_action.index is not None and converted_action.index >= len(
                before_ui_elements
            ):
                print("Index out of range.")
                step_data["summary"] = (
                    "The parameter index is out of range. Remember the index must be in"
                    " the UI element list!"
                )
                self.history.append(step_data)
                return base_agent.AgentInteractionResult(False, step_data)

        if converted_action.action_type == "status":
            if converted_action.goal_status == "infeasible":
                print("Agent stopped since it thinks mission impossible.")
            step_data["summary"] = "Agent thinks the request has been completed."
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(True, step_data)

        if converted_action.action_type == "answer":
            print("Agent answered with: " + converted_action.text)

        ok, target_desc = self._execute_action_and_record(
            converted_action, before_ui_elements, step_data, logical_screen_size
        )
        if not ok:
            self.history.append(step_data)
            return base_agent.AgentInteractionResult(False, step_data)

        # --- Check deferred fast_path: dialog appeared after Action 1? ---
        time.sleep(self.wait_after_action_seconds)
        state = self.env.get_state(wait_to_stabilize=False)
        fp_summary = self._check_and_execute_deferred_fast_path(
            state.ui_elements, step_data, logical_screen_size,
        )
        if fp_summary:
            print(f"[FAST-PATH deferred] {fp_summary}")
            state = self.env.get_state(wait_to_stabilize=False)

        # --- Execute second chained action if present ---
        if second_action_json:
            time.sleep(3.0)  # 3-second interval between chained actions
            print("[CHAIN ACTION] Executing second action after 3s delay...")
            try:
                action2_dict = agent_utils.extract_json(second_action_json)
                action2 = json_action.JSONAction(**action2_dict)
                ok2, target_desc2 = self._execute_action_and_record(
                    action2, state.ui_elements, step_data, logical_screen_size
                )
                if ok2:
                    time.sleep(self.wait_after_action_seconds)
                    state = self.env.get_state(wait_to_stabilize=False)
                    fp_summary2 = self._check_and_execute_deferred_fast_path(
                        state.ui_elements, step_data, logical_screen_size,
                    )
                    if fp_summary2:
                        print(f"[FAST-PATH deferred] {fp_summary2}")
                        state = self.env.get_state(wait_to_stabilize=False)
                    summary = f"Action 1: {action}. Action 2: {second_action_json}. Reason: {reason}"
                    if fp_summary2:
                        summary += f"\n{fp_summary2}"
                    # Wait 3 seconds after second action before proceeding to next step
                    time.sleep(3.0)
                    print("[CHAIN ACTION] Second action executed, waiting 3s before next step...")
                else:
                    summary = f"Action 1: {action}. Action 2 FAILED: {second_action_json}. Reason: {reason}"
            except Exception as e:
                print(f"[CHAIN ACTION] Failed to parse/execute second action: {e}")
                summary = f"Action: {action}. Action 2 parse error. Reason: {reason}"
        else:
            summary = f"Action: {action}. Reason: {reason}"
            if fp_summary:
                summary += f"\n{fp_summary}"

        after_ui_elements = state.ui_elements
        after_ui_elements_list = m3a._generate_ui_elements_description_list(
            after_ui_elements, logical_screen_size
        )
        after_screenshot = state.pixels.copy()
        for index, ui_element in enumerate(after_ui_elements):
            if m3a_utils.validate_ui_element(ui_element, logical_screen_size):
                m3a_utils.add_ui_element_mark(
                    after_screenshot,
                    ui_element,
                    index,
                    logical_screen_size,
                    physical_frame_boundary,
                    orientation,
                )

        m3a_utils.add_screenshot_label(
            step_data["before_screenshot_with_som"], "before"
        )
        m3a_utils.add_screenshot_label(after_screenshot, "after")
        step_data["after_screenshot_with_som"] = after_screenshot.copy()

        step_data["summary_prompt"] = None
        step_data["summary"] = summary
        step_data["summary_raw_response"] = None
        print("Summary: " + summary)

        print(f"[TIMING] STEP {step_num} TOTAL: {time.time()-_step_start:.2f}s")
        print("---")

        self.history.append(step_data)

        return base_agent.AgentInteractionResult(False, step_data)
