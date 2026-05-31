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

"""Composite tasks: Calendar → SMS → Markor information flow.

This is a long-horizon cross-app task requiring the agent to:
1. Open Simple Calendar Pro and CREATE calendar events via GUI
2. Send the event titles via SMS to a phone number
3. Create a summary note in Markor

No SQLite push operations are used for Calendar to avoid the root ownership issue.
The agent must interact with Calendar entirely through GUI operations.
"""

import random
from typing import Any

from android_world.env import interface
from android_world.task_evals import task_eval
from android_world.task_evals.common_validators import sms_validators
from android_world.task_evals.single import markor
from android_world.task_evals.utils import user_data_generation


class CalendarQueryThenSmsAndMarkor(task_eval.TaskEval):
  """Task: Create calendar events via GUI, send titles via SMS, and create a summary note in Markor.

  This is a cross-app task involving:
  1. Simple Calendar Pro: CREATE events for a specific date via GUI operations
  2. Simple SMS Messenger: Send event titles to a phone number
  3. Markor: Create a summary note with the schedule

  The agent must:
  - Open Calendar and create events for the given date with the specified titles
  - Send the event titles to the specified phone number via SMS
  - Create a note in Markor summarizing the day's schedule
  """

  app_names = ("simple calendar pro", "simple sms messenger", "markor")
  complexity = 4.5  # 45 steps

  schema = {
      "type": "object",
      "properties": {
          "year": {"type": "integer"},
          "month": {"type": "integer"},
          "day": {"type": "integer"},
          "event_titles": {"type": "string"},
          "phone_number": {"type": "string"},
          "note_file_name": {"type": "string"},
      },
      "required": [
          "year",
          "month",
          "day",
          "event_titles",
          "phone_number",
          "note_file_name",
      ],
  }

  template = (
      "Create calendar events in Simple Calendar Pro for {year}-{month:02d}-{day:02d}"
      " with the following titles: {event_titles}. Then send the titles of all"
      " events to {phone_number} via Simple SMS Messenger. Finally create a note"
      " in Markor named {note_file_name} with a summary of the day's schedule,"
      " listing all event titles: {event_titles}"
  )

  def __init__(self, params: dict[str, Any]):
    super().__init__(params)
    # Parse event titles (comma-separated)
    self._event_titles = [
        t.strip() for t in params["event_titles"].split(",") if t.strip()
    ]
    self._summary_text = (
        f"Schedule for {params['year']}-{params['month']:02d}-{params['day']:02d}:\n"
        + "\n".join(f"- {t}" for t in self._event_titles)
    )

  def initialize_task(self, env: interface.AsyncEnv) -> None:
    super().initialize_task(env)
    # Clear Calendar to ensure clean state (no SQLite push!)
    from android_world.env import adb_utils
    adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)
    # Initialize SMS task - agent should send the event titles
    self.sms_task = sms_validators.SimpleSMSSendSms(
        params={
            "number": self.params["phone_number"],
            "message": self.params["event_titles"],
        }
    )
    self.sms_task.initialize_task(env)

    # Initialize Markor task - agent needs to create the note
    self.markor_task = markor.MarkorCreateNote(
        params={
            "file_name": self.params["note_file_name"],
            "text": self._summary_text,
        }
    )
    self.markor_task.initialize_task(env)

  def is_successful(self, env: interface.AsyncEnv) -> float:
    super().is_successful(env)
    # Check SMS was sent with event titles (use 60-minute window since task
    # can take 30+ minutes to complete)
    from android_world.task_evals.common_validators import sms_validators
    from android_world.utils import fuzzy_match_lib
    messages = self.sms_task.get_sent_messages(env.controller)
    import time
    time.sleep(5)
    print("\n========== SMS Debug ==========")
    print(f"Target phone: {self.params['phone_number']}")
    print(f"Expected body: {self.params['event_titles']}")
    print(f"All sent messages ({len(messages)}):")
    for msg in messages:
      parsed = sms_validators.parse_message(msg)
      print(f"  Address: {parsed.get('address', 'N/A')}")
      print(f"  Body: {parsed.get('body', 'N/A')}")
      print(f"  Date: {parsed.get('date', 'N/A')}")
      # Check fuzzy match for each matching number
      msg_number = parsed.get("address", "").replace("-", "").replace(" ", "")
      if msg_number == self.params["phone_number"]:
        match_result = fuzzy_match_lib.fuzzy_match(
            parsed.get("body", ""), self.params["event_titles"]
        )
        print(f"  >>> Number matches! fuzzy_match={match_result} (need >= 0.9)")
    sms_was_sent = sms_validators.was_sent(
        messages,
        phone_number=self.params["phone_number"],
        body=self.params["event_titles"],
        current_time_ms=self.sms_task.get_android_time(env.controller),
        time_mins=60,  # Extended window for long-horizon tasks
    )
    print(f"SMS result: {'PASS' if sms_was_sent else 'FAIL'}")
    print("=================================\n")
    # Check Markor note was created - check both .txt and without extension
    from android_world.utils import file_utils
    from android_world.env import adb_utils as adb_utils2
    from android_world.env import device_constants

    # First, list all files in the Markor directory
    print("\n========== Markor Directory Listing ==========")
    ls_res = adb_utils2.issue_generic_request(
        ["shell", "ls", "-la", device_constants.MARKOR_DATA],
        env.controller,
    )
    print(f"Markor directory contents:\n{ls_res.generic.output.decode().replace(chr(13), '')}")
    print("==============================================\n")

    markor_success = False
    # Try with .txt extension (as specified in goal) and without
    for ext in [".txt", ""]:
      full_name = self.params["note_file_name"]
      base_name = full_name
      if ext and not full_name.endswith(ext):
        base_name = full_name + ext
      exists = file_utils.check_file_or_folder_exists(
          base_name, device_constants.MARKOR_DATA, env.controller
      )
      print(f"\n========== Markor Debug (ext='{ext}') ==========")
      print(f"Looking for file: {base_name} in {device_constants.MARKOR_DATA}")
      print(f"File exists: {exists}")
      if exists:
        # Check file contains the event titles (not exact match)
        res = adb_utils2.issue_generic_request(
            ["shell", "cat",
             file_utils.convert_to_posix_path(device_constants.MARKOR_DATA, base_name)],
            env.controller,
        )
        file_contents = res.generic.output.decode().replace("\r", "").strip()
        print(f"File contents: '{file_contents}'")
        for title in self._event_titles:
          found = title in file_contents
          print(f"  Title '{title}' in contents: {found}")
        # Check if all event titles appear in the file
        markor_success = all(
            title in file_contents for title in self._event_titles
        )
        break
    print(f"Markor result: {'PASS' if markor_success else 'FAIL'}")
    print("=================================\n")
    return 1.0 if (sms_was_sent and markor_success) else 0.0

  def tear_down(self, env: interface.AsyncEnv) -> None:
    super().tear_down(env)
    self.sms_task.tear_down(env)
    self.markor_task.tear_down(env)

  @classmethod
  def generate_random_params(cls) -> dict[str, Any]:
    """Generate random parameters for this cross-app task."""
    # Generate 2 random event titles
    n_events = 2
    event_titles = []
    for _ in range(n_events):
      title = user_data_generation.generate_random_string(15)
      # Clean up: replace underscores with spaces, title case
      title = title.replace("_", " ").strip().title()
      if not title:
        title = "Meeting"
      event_titles.append(title)

    # Generate a random 10-digit phone number (digits only)
    phone_number = "+1" + "".join(random.choices("0123456789", k=10))

    note_name = f"schedule_{user_data_generation.generate_random_string(8)}.txt"

    return {
        "year": 2023,
        "month": 10,
        "day": 25,
        "event_titles": ", ".join(event_titles),
        "phone_number": phone_number,
        "note_file_name": note_name,
    }
