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

"""Pega: Long-horizon cross-app tasks grounded in realistic user scenarios.

Each task requires 50+ steps and spans 3+ applications, targeting
four capability dimensions:
1. Cross-App Information Flow
2. Conditional Decision-Making and Error Handling
3. Multi-Subgoal Workflows
4. Long-Horizon Interaction with Exception Handling
"""

import random
from typing import Any

from android_world.env import adb_utils
from android_world.env import device_constants
from android_world.env import interface
from android_world.task_evals import task_eval
from android_world.task_evals.common_validators import file_validators
from android_world.task_evals.common_validators import sms_validators
from android_world.task_evals.utils import user_data_generation
from android_world.utils import fuzzy_match_lib


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

# Map app display names to package names for cleanup
_APP_NAME_TO_PACKAGE = {
    "simple calendar pro": "com.simplemobiletools.calendar.pro",
    "simple sms messenger": "com.simplemobiletools.smsmessenger",
    "markor": "net.gsantner.markor",
    "pro expense": "com.arduia.expense",
    "simple contacts pro": "com.android.contacts",
    "osmand": "net.osmand",
    "simple gallery pro": "com.simplemobiletools.gallery.pro",
    "audio recorder": "com.dimowner.audiorecorder",
    "dialer": "com.android.dialer",
    "broccoli": "org.broccoliapp.broccoli",
    "settings": "com.android.settings",
    "retro music": "code.name.monkey.retromusic",
}


def _clear_task_app_data(app_names: tuple[str, ...], env: interface.AsyncEnv) -> None:
    """Clear app data for all apps used by a task.

    Clears both internal app data (via pm clear) and external storage files
    (for apps like Markor that store files on shared storage).
    Checks if package is installed before attempting to clear, to avoid
    unnecessary retry delays.
    """
    for app_name in app_names:
        pkg = _APP_NAME_TO_PACKAGE.get(app_name.lower().strip())
        if pkg:
            try:
                # Check if package is installed before attempting to clear
                res = adb_utils.issue_generic_request(
                    f"shell pm list packages {pkg}", env.controller,
                )
                if f"package:{pkg}" in res.generic.output.decode():
                    adb_utils.clear_app_data(pkg, env.controller)
            except Exception:
                pass  # App may not be installed or may not have data

    # Also clear external storage directories for specific apps
    external_dirs = {
        "markor": device_constants.MARKOR_DATA,
    }
    for app_name in app_names:
        ext_dir = external_dirs.get(app_name.lower().strip())
        if ext_dir:
            try:
                adb_utils.issue_generic_request(
                    ["shell", "rm", "-rf", ext_dir],
                    env.controller,
                )
            except Exception:
                pass

    # Clear SMS database if SMS app was used
    sms_app_names = {"simple sms messenger", "simple sms"}
    if any(app_name.lower() in sms_app_names for app_name in app_names):
        sms_db = "/data/data/com.android.providers.telephony/databases/mmssms.db"
        try:
            adb_utils.execute_sql_command(sms_db, "DELETE FROM sms;", env.controller)
            adb_utils.execute_sql_command(sms_db, "DELETE FROM threads;", env.controller)
        except Exception:
            pass


def _generate_random_phone() -> str:
    return "+1" + "".join(random.choices("0123456789", k=10))


def _generate_random_date() -> dict:
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    year = random.choice([2023, 2024, 2025])
    return {"year": year, "month": month, "day": day,
            "date_str": f"{year}-{month:02d}-{day:02d}"}


def _generate_random_contacts(n: int = 2) -> list[dict]:
    contacts = []
    for _ in range(n):
        name = user_data_generation.generate_random_string(8).title()
        contacts.append({
            "name": name,
            "phone": _generate_random_phone(),
            "email": f"{name.lower()}@example.com",
        })
    return contacts


def _check_sms_sent(env, phone: str, keyword: str, time_mins: int = 60) -> bool:
    """Check if an SMS was sent to phone containing the keyword.

    Uses substring matching instead of fuzzy_match to handle cases where
    the agent sends a longer message that includes the keyword.
    """
    messages = _get_sent_messages(env)
    current_time = _get_android_time(env)
    n_minutes_ms = time_mins * 60 * 1000

    print(f"[SMS CHECK] Looking for phone={phone}, keyword='{keyword}'")
    print(f"[SMS CHECK] Total messages in DB: {len(messages)}")

    for message in messages:
        fields = sms_validators.parse_message(message)
        try:
            msg_number = fields["address"].replace("-", "").replace(" ", "")
            msg_body = fields["body"]
            msg_date = int(fields["date"])
        except KeyError:
            continue

        print(f"[SMS CHECK] Found msg: number={msg_number}, body='{msg_body[:50]}...'")
        if msg_number != phone:
            print(f"[SMS CHECK]   -> phone mismatch")
            continue
        if current_time - msg_date > n_minutes_ms:
            print(f"[SMS CHECK]   -> too old")
            continue
        # Use substring matching instead of fuzzy_match
        if keyword.lower() in msg_body.lower():
            print(f"[SMS CHECK]   -> MATCH!")
            return True
        else:
            print(f"[SMS CHECK]   -> keyword not in body")
    print(f"[SMS CHECK] No matching SMS found.")
    return False


def _get_sent_messages(env):
    response = adb_utils.issue_generic_request(
        "shell content query --uri content://sms/sent".split(),
        env.controller,
    )
    if (response.generic.output.decode()
            .replace("\r", "").startswith("No result found.")):
        return []
    messages = response.generic.output.split(b"\nRow:")
    for i, m in enumerate(messages):
        if i > 0:
            messages[i] = b"Row:" + m
    return [m.decode() for m in messages]


def _get_android_time(env) -> int:
    output = adb_utils.issue_generic_request(
        ["shell", "date", "+%s"], env.controller,
    )
    return int(output.generic.output.strip()) * 1000


def _check_file_contains(
    env, file_name: str, dir_path: str, required_substrings: list[str],
) -> bool:
    """Check if file exists and contains all required substrings."""
    from android_world.utils import file_utils
    # Try with and without .txt extension
    for name in [file_name, file_name.replace(".txt", "") + ".txt",
                 file_name.replace(".md", "") + ".md", file_name + ".txt"]:
        exists = file_utils.check_file_or_folder_exists(
            name, dir_path, env.controller,
        )
        if exists:
            res = adb_utils.issue_generic_request(
                ["shell", "cat",
                 file_utils.convert_to_posix_path(dir_path, name)],
                env.controller,
            )
            content = res.generic.output.decode().replace("\r", "").strip()
            print(f"[FILE CHECK] Found '{name}', content='{content[:100]}...'")
            return all(sub in content for sub in required_substrings)
    # Also try fuzzy filename matching for mangled names
    ls_res = adb_utils.issue_generic_request(
        ["shell", "ls", dir_path], env.controller,
    )
    files = ls_res.generic.output.decode().replace("\r", "").strip().split("\n")
    print(f"[FILE CHECK] dir={dir_path}, files={files}")
    base = file_name.replace(".txt", "").replace(".md", "")
    for f in files:
        f = f.strip()
        if f and base in f:
            res = adb_utils.issue_generic_request(
                ["shell", "cat",
                 file_utils.convert_to_posix_path(dir_path, f)],
                env.controller,
            )
            content = res.generic.output.decode().replace("\r", "").strip()
            print(f"[FILE CHECK] Fuzzy match '{f}', content='{content[:100]}...'")
            return all(sub in content for sub in required_substrings)
    return False


def _check_wifi_on(env) -> bool:
    res = adb_utils.issue_generic_request(
        ["shell", "settings get global wifi_on"], env.controller,
    )
    return res.generic.output.decode().strip() in ("1", "2")


def _check_bluetooth_on(env) -> bool:
    res = adb_utils.issue_generic_request(
        ["shell", "settings get global bluetooth_on"], env.controller,
    )
    return res.generic.output.decode().strip() == "1"


# ===========================================================================
# Category 1: Cross-App Information Flow
# ===========================================================================


class BusinessTripPlanning(task_eval.TaskEval):
    """Task: Plan a business trip across Calendar, Maps, Notes, and SMS."""

    app_names = ("simple calendar pro", "markor", "simple sms messenger")
    complexity = 8.0  # 80 steps
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": ["city", "start_date", "end_date", "phone", "note_file_name"],
    }
    template = (
        "You are going on a business trip to {city} from {start_date} to "
        "{end_date}. Complete the following: 1) Create a calendar event in "
        "Simple Calendar Pro titled 'Business Trip - {city}' spanning these dates. "
        "2) Create a note in Markor named {note_file_name} recording the trip "
        "dates and destination. 3) Send the trip details via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        # Check calendar event exists (via GUI state or sqlite)
        calendar_success = _check_calendar_has_event(
            env, keyword="Business Trip",
        )
        # Check Markor note
        markor_success = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["city"], self.params["start_date"], self.params["end_date"]],
        )
        # Check SMS
        sms_body = (
            f"Business trip to {self.params['city']} "
            f"from {self.params['start_date']} to {self.params['end_date']}"
        )
        sms_success = _check_sms_sent(
            env, self.params["phone"], sms_body, time_mins=60,
        )
        scores = [calendar_success, markor_success, sms_success]
        if any(not s for s in scores):
            print(f"\n[FAIL DETAILS] calendar={calendar_success}, "
                  f"markor={markor_success}, sms={sms_success}")
        return sum(scores) / len(scores)

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        d2 = _generate_random_date()
        # Ensure end >= start
        city = user_data_generation.generate_random_string(8).title()
        note_name = f"trip_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "city": city,
            "start_date": d["date_str"],
            "end_date": d2["date_str"],
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class ExpenseReimbursement(task_eval.TaskEval):
    """Task: Create expense notes, enter them in Expense app, notify finance."""

    app_names = ("markor", "pro expense", "simple sms messenger")
    complexity = 5.5
    schema = {
        "type": "object",
        "properties": {
            "note_file_name": {"type": "string"},
            "item1": {"type": "string"},
            "item2": {"type": "string"},
            "item3": {"type": "string"},
            "price1": {"type": "number"},
            "price2": {"type": "number"},
            "price3": {"type": "number"},
            "phone": {"type": "string"},
            "total": {"type": "number"},
        },
        "required": [
            "note_file_name", "item1", "item2", "item3",
            "price1", "price2", "price3", "phone", "total",
        ],
    }
    template = (
        "You need to reimburse business expenses. 1) Create a note in Markor "
        "named {note_file_name} listing three expenses: {item1} ${price1}, "
        "{item2} ${price2}, {item3} ${price3}. 2) Add each expense in Pro Expense "
        "with the correct amount and description. 3) Send the total reimbursement "
        "amount ${total} via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        # Check note exists with expense details
        note_success = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["item1"], self.params["item2"], self.params["item3"]],
        )
        # Check SMS
        sms_body = f"Total reimbursement: ${self.params['total']:.2f} USD"
        sms_success = _check_sms_sent(
            env, self.params["phone"], sms_body, time_mins=60,
        )
        if not note_success or not sms_success:
            print(f"\n[FAIL DETAILS] note={note_success}, sms={sms_success}")
        return (note_success + sms_success) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        items = [
            user_data_generation.generate_random_string(6).title(),
            user_data_generation.generate_random_string(6).title(),
            user_data_generation.generate_random_string(6).title(),
        ]
        prices = [round(random.uniform(10, 200), 2) for _ in range(3)]
        total = round(sum(prices), 2)
        note_name = f"reimbursement_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "note_file_name": note_name,
            "item1": items[0], "item2": items[1], "item3": items[2],
            "price1": prices[0], "price2": prices[1], "price3": prices[2],
            "phone": _generate_random_phone(),
            "total": total,
        }


class PartyPlanning(task_eval.TaskEval):
    """Task: Plan a party - calendar event, guest contacts, note, invitations."""

    app_names = ("simple calendar pro", "simple contacts pro",
                 "markor", "simple sms messenger")
    complexity = 7.0
    schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "time": {"type": "string"},
            "guest_name": {"type": "string"},
            "guest_phone": {"type": "string"},
            "note_file_name": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["date", "time", "guest_name", "guest_phone",
                     "note_file_name", "phone"],
    }
    template = (
        "You are planning a birthday party. 1) Create a calendar event "
        "'Birthday Party' on {date} at {time} in Simple Calendar Pro. "
        "2) Add a contact named {guest_name} with phone {guest_phone} in "
        "Simple Contacts Pro. 3) Create a note in Markor named {note_file_name} "
        "listing the guest name and party details. 4) Send an SMS invitation "
        "to {guest_phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        cal = _check_calendar_has_event(env, keyword="Birthday Party")
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["guest_name"], self.params["date"]],
        )
        sms = _check_sms_sent(
            env, self.params["guest_phone"], "invited", time_mins=60,
        )
        if not cal or not note or not sms:
            print(f"\n[FAIL DETAILS] calendar={cal}, note={note}, sms={sms}")
        return (cal + note + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        h = random.randint(10, 20)
        m = random.choice([0, 30])
        guest = user_data_generation.generate_random_string(7).title()
        note_name = f"party_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "date": d["date_str"],
            "time": f"{h:02d}:{m:02d}",
            "guest_name": guest,
            "guest_phone": _generate_random_phone(),
            "note_file_name": note_name,
            "phone": _generate_random_phone(),
        }


class MedicalAppointmentWorkflow(task_eval.TaskEval):
    """LDP Task: Rules are hidden in a separate file, forcing the agent to read and apply them."""

    app_names = ("markor", "simple calendar pro",
                 "simple contacts pro", "simple sms messenger")
    complexity = 5.5
    schema = {
        "type": "object",
        "properties": {
            "symptom_file": {"type": "string"},
            "rule_file": {"type": "string"},
            "date": {"type": "string"},
            "time": {"type": "string"},
            "clinic_name": {"type": "string"},
            "doctor_name": {"type": "string"},
            "clinic_phone": {"type": "string"},
            "family_phone": {"type": "string"},
        },
        "required": [
            "symptom_file", "rule_file", "date", "time", "clinic_name", "doctor_name",
            "clinic_phone", "family_phone",
        ],
    }
    template = (
        "You are not feeling well. Open Markor and read the note "
        "{symptom_file} which lists your symptoms. "
        "Then read the note {rule_file} to see the clinic's booking policy. "
        "Follow the policy strictly to book the correct appointment in Calendar "
        "(either 'Specialist Appointment' or 'Routine Checkup') on {date} at {time}. "
        "Then add contact 'Dr. {doctor_name}' with phone {clinic_phone} "
        "in Simple Contacts Pro, and send SMS to {family_phone} about your appointment."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

        # Pre-create files in Markor
        adb_utils.issue_generic_request(
            ["shell", "mkdir", "-p", device_constants.MARKOR_DATA],
            env.controller,
        )
        # 1. Symptom file
        symptoms = self.params["symptoms_list"]
        symptom_content = "\\n".join(symptoms)
        symptom_path = f"{device_constants.MARKOR_DATA}/{self.params['symptom_file']}"
        adb_utils.issue_generic_request(
            f"shell echo -e '{symptom_content}' > {symptom_path}",
            env.controller,
        )
        # 2. Rule file (HIDDEN LOGIC)
        rule_content = (
            "Clinic Booking Policy:\\n"
            "- If patient has MORE than 3 symptoms: Book 'Specialist Appointment'.\\n"
            "- If patient has 3 or fewer symptoms: Book 'Routine Checkup'."
        )
        rule_path = f"{device_constants.MARKOR_DATA}/{self.params['rule_file']}"
        adb_utils.issue_generic_request(
            f"shell echo -e '{rule_content}' > {rule_path}",
            env.controller,
        )

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        # Determine expected behavior based on hidden symptom count
        num_symptoms = len(self.params["symptoms_list"])
        expected_keyword = "Specialist Appointment" if num_symptoms > 3 else "Routine Checkup"

        cal = _check_calendar_has_event(env, keyword=expected_keyword)
        sms = _check_sms_sent(
            env, self.params["family_phone"], "appointment", time_mins=60,
        )
        if not cal or not sms:
            print(f"\n[FAIL DETAILS] calendar (expected '{expected_keyword}')={cal}, sms={sms}")
        return (cal + sms) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        h = random.randint(8, 17)
        n_symptoms = random.randint(2, 5)  # 2-5 symptoms to trigger either branch
        symptoms = [
            user_data_generation.generate_random_string(6).title()
            for _ in range(n_symptoms)
        ]
        doctor = user_data_generation.generate_random_string(6).title()
        clinic = user_data_generation.generate_random_string(8).title() + " Clinic"
        symptom_file = f"my_symptoms_{user_data_generation.generate_random_string(6)}.txt"
        rule_file = f"clinic_rules_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "symptom_file": symptom_file,
            "rule_file": rule_file,
            "symptoms_list": symptoms,  # Hidden from Goal
            "date": d["date_str"], "time": f"{h:02d}:00",
            "clinic_name": clinic,
            "doctor_name": doctor,
            "clinic_phone": _generate_random_phone(),
            "family_phone": _generate_random_phone(),
        }


class PhotoMemorySharing(task_eval.TaskEval):
    """Task: View photos, write travel diary, create calendar event, share via SMS."""

    app_names = ("simple gallery pro", "markor",
                 "simple calendar pro", "simple sms messenger")
    complexity = 6.0
    schema = {
        "type": "object",
        "properties": {
            "destination": {"type": "string"},
            "spot1": {"type": "string"},
            "spot2": {"type": "string"},
            "spot3": {"type": "string"},
            "date": {"type": "string"},
            "note_file_name": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": [
            "destination", "spot1", "spot2", "spot3",
            "date", "note_file_name", "phone",
        ],
    }
    template = (
        "You just returned from a trip to {destination}. 1) View photos in "
        "Simple Gallery Pro. 2) Create a travel diary note in Markor named "
        "{note_file_name} listing places visited: {spot1}, {spot2}, {spot3}. "
        "3) Create a calendar event 'Trip to {destination} Ended' on {date}. "
        "4) Send the diary content via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["spot1"], self.params["spot2"],
             self.params["spot3"], self.params["destination"]],
        )
        cal = _check_calendar_has_event(
            env, keyword="Trip to",
        )
        sms = _check_sms_sent(
            env, self.params["phone"], self.params["destination"], time_mins=60,
        )
        if not note or not cal or not sms:
            print(f"\n[FAIL DETAILS] note={note}, calendar={cal}, sms={sms}")
        return (note + cal + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        dest = user_data_generation.generate_random_string(8).title()
        spots = [
            user_data_generation.generate_random_string(6).title(),
            user_data_generation.generate_random_string(6).title(),
            user_data_generation.generate_random_string(6).title(),
        ]
        note_name = f"diary_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "destination": dest,
            "spot1": spots[0], "spot2": spots[1], "spot3": spots[2],
            "date": d["date_str"],
            "note_file_name": note_name,
            "phone": _generate_random_phone(),
        }


# ===========================================================================
# Category 2: Conditional Decision-Making and Error Handling
# ===========================================================================


class NetworkTroubleshooting(task_eval.TaskEval):
    """Task: Check network state, fix issues, log actions, report via SMS."""

    app_names = ("settings", "markor", "simple sms messenger")
    complexity = 5.5
    schema = {
        "type": "object",
        "properties": {
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": ["phone", "note_file_name"],
    }
    template = (
        "Your network connection seems broken. 1) Check and fix WiFi (turn it on "
        "if off). 2) Turn off Bluetooth (it may interfere). 3) Create a note in "
        "Markor named {note_file_name} recording what you found and what you did. "
        "4) Send the results via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        # WiFi should be on
        wifi = _check_wifi_on(env)
        # Bluetooth should be off
        bt_off = not _check_bluetooth_on(env)
        # Note should exist with keywords
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            ["WiFi", "Bluetooth"],
        )
        # SMS should be sent
        sms = _check_sms_sent(
            env, self.params["phone"], "WiFi", time_mins=60,
        )
        if not wifi or not bt_off or not note or not sms:
            print(f"\n[FAIL DETAILS] wifi={wifi}, bt_off={bt_off}, "
                  f"note={note}, sms={sms}")
        return (wifi + bt_off + note + sms) / 4.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        note_name = f"network_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class CalendarConflictResolution(task_eval.TaskEval):
    """Task: Check calendar for existing events, consolidate or create new ones."""

    app_names = ("simple calendar pro", "markor", "simple sms messenger")
    complexity = 5.5
    schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "note_file_name": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["date", "note_file_name", "phone"],
    }
    template = (
        "Organize your schedule for {date}. 1) Check Simple Calendar Pro for "
        "existing events on this date. 2) If events exist, delete them and create "
        "a new consolidated event; if not, create 'Team Meeting' and 'Client Call'. "
        "3) Record the final schedule in Markor note {note_file_name}. "
        "4) Send the schedule via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        cal = _check_calendar_has_event(env, keyword="Meeting")
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["date"]],
        )
        sms = _check_sms_sent(
            env, self.params["phone"], "Meeting", time_mins=60,
        )
        if not cal or not note or not sms:
            print(f"\n[FAIL DETAILS] calendar={cal}, note={note}, sms={sms}")
        return (cal + note + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        note_name = f"schedule_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "date": d["date_str"],
            "note_file_name": note_name,
            "phone": _generate_random_phone(),
        }


class StorageSpaceManagement(task_eval.TaskEval):
    """Task: Check storage, clean up old notes, report results."""

    app_names = ("markor", "simple gallery pro", "simple sms messenger")
    complexity = 5.5
    schema = {
        "type": "object",
        "properties": {
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": ["phone", "note_file_name"],
    }
    template = (
        "Your phone is running low on storage. 1) Check existing notes in Markor "
        "and clean up old ones if there are too many. 2) Check photo count in "
        "Simple Gallery Pro. 3) Create a cleanup report in Markor named "
        "{note_file_name}. 4) Send the report via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            ["cleanup", "note"],
        )
        sms = _check_sms_sent(
            env, self.params["phone"], "clean", time_mins=60,
        )
        if not note or not sms:
            print(f"\n[FAIL DETAILS] note={note}, sms={sms}")
        return (note + sms) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        note_name = f"storage_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class MissedCallFollowUp(task_eval.TaskEval):
    """Task: Check missed calls, handle contact, send follow-up SMS, log."""

    app_names = ("dialer", "simple contacts pro",
                 "simple sms messenger", "markor")
    complexity = 5.5
    schema = {
        "type": "object",
        "properties": {
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": ["phone", "note_file_name"],
    }
    template = (
        "You have a missed call. 1) Check the Phone app for missed calls. "
        "2) Look up or create a contact for the caller. 3) Send an SMS saying "
        "'Sorry I missed your call' to the caller's number. 4) Log the action "
        "in Markor note {note_file_name}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            ["missed", "call"],
        )
        # SMS with "missed" keyword
        sms = _check_sms_sent(
            env, self.params["phone"], "missed", time_mins=60,
        )
        if not note or not sms:
            print(f"\n[FAIL DETAILS] note={note}, sms={sms}")
        return (note + sms) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        note_name = f"missed_call_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class BudgetCheckBeforePurchase(task_eval.TaskEval):
    """LDP Task: Read budget/shopping from Markor, decide to buy or warn."""

    app_names = ("pro expense", "markor",
                 "simple calendar pro", "simple sms messenger")
    complexity = 8.0  # 80 steps
    schema = {
        "type": "object",
        "properties": {
            "budget_file": {"type": "string"},
            "shopping_file": {"type": "string"},
            "policy_file": {"type": "string"},
            "item1": {"type": "string"},
            "item2": {"type": "string"},
            "item3": {"type": "string"},
            "price1": {"type": "number"},
            "price2": {"type": "number"},
            "price3": {"type": "number"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "budget_file", "shopping_file", "policy_file",
            "item1", "item2", "item3",
            "price1", "price2", "price3", "phone", "note_file_name",
        ],
    }
    template = (
        "You want to buy some items. Open Markor and read three notes: "
        "{budget_file}, {shopping_file}, and {policy_file}. "
        "Follow the financial policy strictly to decide what to do. "
        "Finally, send an SMS to {phone} with the budget status."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

        # Pre-create budget file in Markor
        budget = self.params["budget_limit"]
        adb_utils.issue_generic_request(
            ["shell", "mkdir", "-p", device_constants.MARKOR_DATA],
            env.controller,
        )
        budget_path = f"{device_constants.MARKOR_DATA}/{self.params['budget_file']}"
        adb_utils.issue_generic_request(
            f"shell echo 'Budget Limit: ${budget}' > {budget_path}",
            env.controller,
        )

        # Pre-create shopping list file in Markor
        items = self.params["shopping_items"]  # List of (name, price) tuples
        shopping_content = "Shopping List:\\n" + "\\n".join(
            f"- {name}: ${price}" for name, price in items
        )
        shopping_path = f"{device_constants.MARKOR_DATA}/{self.params['shopping_file']}"
        adb_utils.issue_generic_request(
            f"shell echo -e '{shopping_content}' > {shopping_path}",
            env.controller,
        )

        # Pre-create policy file in Markor (HIDDEN LOGIC)
        policy_content = (
            "Financial Policy:\\n"
            "- Calculate total cost of all items in Shopping List.\\n"
            "- If Total Cost <= Budget Limit: Add all items to Pro Expense. Create note 'budget_summary.txt'. Send SMS 'Budget OK'.\\n"
            "- If Total Cost > Budget Limit: DO NOT buy anything. Create note 'over_budget_warning.txt' with 'Warning: Over Budget'. Send SMS 'Need Loan'."
        )
        policy_path = f"{device_constants.MARKOR_DATA}/{self.params['policy_file']}"
        adb_utils.issue_generic_request(
            f"shell echo -e '{policy_content}' > {policy_path}",
            env.controller,
        )

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        total = self.params["price1"] + self.params["price2"] + self.params["price3"]
        budget = self.params["budget_limit"]
        should_buy = total <= budget

        # Check SMS (always expected)
        expected_sms_keyword = "Budget OK" if should_buy else "Loan"
        sms = _check_sms_sent(
            env, self.params["phone"], expected_sms_keyword, time_mins=60,
        )

        if should_buy:
            # Check summary note exists
            note = _check_file_contains(
                env, "budget_summary.txt",
                device_constants.MARKOR_DATA,
                ["budget"],
            )
            cal = _check_calendar_has_event(env, keyword="Review")
            if not note or not cal or not sms:
                print(f"\n[FAIL DETAILS] buy mode: note={note}, calendar={cal}, sms={sms}")
            return (note + cal + sms) / 3.0
        else:
            # Check warning note exists
            warn = _check_file_contains(
                env, "over_budget_warning.txt",
                device_constants.MARKOR_DATA,
                ["over", "budget"],
            )
            if not warn or not sms:
                print(f"\n[FAIL DETAILS] warn mode: warning_note={warn}, sms={sms}")
            return (warn + sms) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        items = [
            user_data_generation.generate_random_string(6).title(),
            user_data_generation.generate_random_string(6).title(),
            user_data_generation.generate_random_string(6).title(),
        ]
        prices = [round(random.uniform(20, 100), 2) for _ in range(3)]
        total = sum(prices)
        # 50% chance budget is enough, 50% chance it's not
        if random.random() < 0.5:
            budget = round(total * 1.5, 2)  # Budget is enough
        else:
            budget = round(total * 0.5, 2)  # Budget is NOT enough

        note_name = f"budget_{user_data_generation.generate_random_string(6)}.txt"
        budget_file = f"my_budget_{user_data_generation.generate_random_string(6)}.txt"
        shopping_file = f"shopping_list_{user_data_generation.generate_random_string(6)}.txt"
        policy_file = f"financial_policy_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "budget_file": budget_file,
            "shopping_file": shopping_file,
            "policy_file": policy_file,
            "budget_limit": budget,
            "shopping_items": list(zip(items, prices)),  # Hidden from Goal
            "item1": items[0], "item2": items[1], "item3": items[2],
            "price1": prices[0], "price2": prices[1], "price3": prices[2],
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


# ===========================================================================
# Category 3: Multi-Subgoal Workflows
# ===========================================================================


class NewJobOnboarding(task_eval.TaskEval):
    """Task: New job setup - add HR contact, create work schedule, note, confirm."""

    app_names = ("simple contacts pro", "simple calendar pro",
                 "markor", "simple sms messenger")
    complexity = 6.5
    schema = {
        "type": "object",
        "properties": {
            "hr_name": {"type": "string"},
            "hr_phone": {"type": "string"},
            "hr_email": {"type": "string"},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
            "your_name": {"type": "string"},
        },
        "required": [
            "hr_name", "hr_phone", "hr_email",
            "start_time", "end_time", "phone",
            "note_file_name", "your_name",
        ],
    }
    template = (
        "You just started a new job. 1) Add HR contact {hr_name} with phone "
        "{hr_phone} and email {hr_email} in Simple Contacts Pro. 2) Create "
        "daily 'Work Day' events in Simple Calendar Pro from {start_time} to "
        "{end_time}. 3) Create a note in Markor named {note_file_name} with "
        "HR contact info and work schedule. 4) Send confirmation SMS to "
        "{hr_phone}: 'Hi {hr_name}, this is {your_name}. I completed my setup.'"
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["hr_name"], self.params["hr_phone"]],
        )
        sms = _check_sms_sent(
            env, self.params["hr_phone"], "setup", time_mins=60,
        )
        cal = _check_calendar_has_event(env, keyword="Work Day")
        if not note or not sms or not cal:
            print(f"\n[FAIL DETAILS] note={note}, sms={sms}, calendar={cal}")
        return (note + sms + cal) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        hr = user_data_generation.generate_random_string(7).title()
        note_name = f"work_info_{user_data_generation.generate_random_string(6)}.txt"
        your = user_data_generation.generate_random_string(7).title()
        return {
            "hr_name": hr,
            "hr_phone": _generate_random_phone(),
            "hr_email": f"{hr.lower()}@company.com",
            "start_time": "09:00",
            "end_time": "17:00",
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
            "your_name": your,
        }


class WeeklyMealPrep(task_eval.TaskEval):
    """Task: Weekly meal planning - menu note, expense, calendar events, SMS."""

    app_names = ("markor", "pro expense",
                 "simple calendar pro", "simple sms messenger")
    complexity = 7.0
    schema = {
        "type": "object",
        "properties": {
            "dish1": {"type": "string"},
            "dish2": {"type": "string"},
            "dish3": {"type": "string"},
            "dish4": {"type": "string"},
            "dish5": {"type": "string"},
            "grocery_cost": {"type": "number"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "dish1", "dish2", "dish3", "dish4", "dish5",
            "grocery_cost", "phone", "note_file_name",
        ],
    }
    template = (
        "Plan your weekly meals. 1) Create a note in Markor named {note_file_name} "
        "with a 5-day meal menu: {dish1}, {dish2}, {dish3}, {dish4}, {dish5}. "
        "2) Add a grocery expense of ${grocery_cost} in Pro Expense. "
        "3) Create calendar events for each meal. 4) Send the weekly menu "
        "via SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["dish1"], self.params["dish2"],
             self.params["dish3"], self.params["dish4"],
             self.params["dish5"]],
        )
        sms = _check_sms_sent(
            env, self.params["phone"], self.params["dish1"], time_mins=60,
        )
        if not note or not sms:
            print(f"\n[FAIL DETAILS] note={note}, sms={sms}")
        return (note + sms) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        dishes = [
            user_data_generation.generate_random_string(8).title(),
            user_data_generation.generate_random_string(8).title(),
            user_data_generation.generate_random_string(8).title(),
            user_data_generation.generate_random_string(8).title(),
            user_data_generation.generate_random_string(8).title(),
        ]
        cost = round(random.uniform(30, 150), 2)
        note_name = f"meal_plan_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "dish1": dishes[0], "dish2": dishes[1], "dish3": dishes[2],
            "dish4": dishes[3], "dish5": dishes[4],
            "grocery_cost": cost,
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class HomeRenovationProject(task_eval.TaskEval):
    """LDP Task: Check Calendar load, decide full or partial renovation."""

    app_names = ("markor", "simple calendar pro", "pro expense",
                 "simple contacts pro", "simple sms messenger")
    complexity = 7.5
    schema = {
        "type": "object",
        "properties": {
            "task1": {"type": "string"},
            "task2": {"type": "string"},
            "task3": {"type": "string"},
            "date1": {"type": "string"},
            "date2": {"type": "string"},
            "date3": {"type": "string"},
            "paint_cost": {"type": "number"},
            "floor_cost": {"type": "number"},
            "plumb_cost": {"type": "number"},
            "worker_name": {"type": "string"},
            "worker_phone": {"type": "string"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
            "instruction_file": {"type": "string"},
        },
        "required": [
            "task1", "task2", "task3",
            "date1", "date2", "date3",
            "paint_cost", "floor_cost", "plumb_cost",
            "worker_name", "worker_phone", "phone", "note_file_name",
            "instruction_file",
        ],
    }
    template = (
        "You are doing home renovation. First, check the Calendar to see how busy "
        "you are this month. Then read the note {instruction_file} in Markor "
        "to see the contractor's scheduling guidelines. "
        "Follow the guidelines strictly to decide which tasks to schedule. "
        "Finally, add worker contact '{worker_name}' with phone {worker_phone} "
        "and send SMS to {phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

        # Pre-create instruction file in Markor (HIDDEN LOGIC)
        adb_utils.issue_generic_request(
            ["shell", "mkdir", "-p", device_constants.MARKOR_DATA],
            env.controller,
        )
        instruction_content = (
            "Contractor Scheduling Guidelines:\\n"
            "- Check how many events are in the Calendar for this month.\\n"
            "- If there are MORE than 5 events (you are busy): ONLY schedule the most urgent task '{task1}'.\\n"
            "- If there are 5 or fewer events (you have time): Schedule ALL tasks ({task1}, {task2}, {task3})."
        )
        instruction_path = f"{device_constants.MARKOR_DATA}/{self.params['instruction_file']}"
        adb_utils.issue_generic_request(
            f"shell echo -e '{instruction_content}' > {instruction_path}",
            env.controller,
        )

        # Pre-seed Calendar with random events to trigger either branch
        n_events = self.params["seed_events"]
        for i in range(n_events):
            adb_utils.issue_generic_request(
                ["shell", "content", "insert", "--uri",
                 "content://com.android.calendar/events",
                 "--bind", f"title:s:Preset Event {i+1}",
                 "--bind", f"dtstart:i:{random.randint(1700000000000, 1800000000000)}",
                 "--bind", f"dtend:i:{random.randint(1700000000000, 1800000000000)}"],
                env.controller,
            )

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        seed_events = self.params["seed_events"]
        should_do_all = seed_events <= 5

        if should_do_all:
            note = _check_file_contains(
                env, self.params["note_file_name"],
                device_constants.MARKOR_DATA,
                [self.params["task1"], self.params["task2"],
                 self.params["task3"]],
            )
            cal = _check_calendar_has_event(env, keyword=self.params["task1"])
            sms = _check_sms_sent(
                env, self.params["phone"], self.params["task1"], time_mins=60,
            )
            if not note or not cal or not sms:
                print(f"\n[FAIL DETAILS] full mode: note={note}, calendar={cal}, sms={sms}")
            return (note + cal + sms) / 3.0
        else:
            note = _check_file_contains(
                env, self.params["note_file_name"],
                device_constants.MARKOR_DATA,
                [self.params["task1"]],
            )
            sms = _check_sms_sent(
                env, self.params["phone"], self.params["task1"], time_mins=60,
            )
            if not note or not sms:
                print(f"\n[FAIL DETAILS] partial mode: note={note}, sms={sms}")
            return (note + sms) / 2.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        tasks = ["Painting", "Flooring", "Plumbing"]
        dates = [_generate_random_date() for _ in range(3)]
        costs = [round(random.uniform(100, 500), 2) for _ in range(3)]
        worker = user_data_generation.generate_random_string(7).title()
        note_name = f"renovation_{user_data_generation.generate_random_string(6)}.txt"
        instruction_file = f"contractor_instructions_{user_data_generation.generate_random_string(6)}.txt"
        seed_events = random.randint(3, 8)
        return {
            "task1": tasks[0], "task2": tasks[1], "task3": tasks[2],
            "date1": dates[0]["date_str"],
            "date2": dates[1]["date_str"],
            "date3": dates[2]["date_str"],
            "paint_cost": costs[0], "floor_cost": costs[1],
            "plumb_cost": costs[2],
            "worker_name": worker,
            "worker_phone": _generate_random_phone(),
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
            "instruction_file": instruction_file,
            "seed_events": seed_events,
        }


class StudentExamPrep(task_eval.TaskEval):
    """Task: Student exam prep - study schedule, notes, classmates, messages."""

    app_names = ("simple calendar pro", "markor",
                 "simple contacts pro", "simple sms messenger")
    complexity = 6.5
    schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "start_date": {"type": "string"},
            "exam_date": {"type": "string"},
            "classmate1": {"type": "string"},
            "phone1": {"type": "string"},
            "classmate2": {"type": "string"},
            "phone2": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "subject", "start_date", "exam_date",
            "classmate1", "phone1", "classmate2", "phone2",
            "note_file_name",
        ],
    }
    template = (
        "You have an exam coming up. 1) Create daily 'Study {subject}' events "
        "in Simple Calendar Pro from {start_date} to {exam_date}. 2) Create "
        "study notes in Markor named {note_file_name} with at least 5 key points. "
        "3) Add classmates '{classmate1}' ({phone1}) and '{classmate2}' ({phone2}) "
        "in Simple Contacts Pro. 4) Send study invitation SMS to both classmates."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        cal = _check_calendar_has_event(
            env, keyword=f"Study {self.params['subject']}",
        )
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["subject"]],
        )
        sms = _check_sms_sent(
            env, self.params["phone1"], "study", time_mins=60,
        )
        if not cal or not note or not sms:
            print(f"\n[FAIL DETAILS] calendar={cal}, note={note}, sms={sms}")
        return (cal + note + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d1 = _generate_random_date()
        d2 = _generate_random_date()
        subject = random.choice(["Math", "Physics", "Chemistry", "Biology",
                                 "Computer Science", "History"])
        c1 = user_data_generation.generate_random_string(7).title()
        c2 = user_data_generation.generate_random_string(7).title()
        note_name = f"study_{subject.lower()}_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "subject": subject,
            "start_date": d1["date_str"],
            "exam_date": d2["date_str"],
            "classmate1": c1, "phone1": _generate_random_phone(),
            "classmate2": c2, "phone2": _generate_random_phone(),
            "note_file_name": note_name,
        }


class FitnessGoalTracking(task_eval.TaskEval):
    """Task: Fitness plan - goal note, schedule, gym expense, coach contact, SMS."""

    app_names = ("markor", "simple calendar pro", "pro expense",
                 "simple contacts pro", "simple sms messenger")
    complexity = 7.0
    schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "coach_name": {"type": "string"},
            "coach_phone": {"type": "string"},
            "gym_cost": {"type": "number"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "goal", "coach_name", "coach_phone",
            "gym_cost", "phone", "note_file_name",
        ],
    }
    template = (
        "You are starting a new fitness plan. 1) Create a fitness plan note in "
        "Markor named {note_file_name} with goal '{goal}' and a weekly schedule. "
        "2) Create daily workout events in Simple Calendar Pro. 3) Add gym "
        "membership expense ${gym_cost} in Pro Expense. 4) Add coach contact "
        "'{coach_name}' with phone {coach_phone}. 5) Send a message to your "
        "coach {coach_phone} about starting the plan."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["goal"]],
        )
        sms = _check_sms_sent(
            env, self.params["coach_phone"], "fitness", time_mins=60,
        )
        cal = _check_calendar_has_event(env, keyword="workout")
        if not note or not sms or not cal:
            print(f"\n[FAIL DETAILS] note={note}, sms={sms}, calendar={cal}")
        return (note + sms + cal) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        goal = random.choice(["Lose Weight", "Build Muscle",
                              "Run a Marathon", "Improve Flexibility"])
        coach = user_data_generation.generate_random_string(7).title()
        cost = round(random.uniform(30, 150), 2)
        note_name = f"fitness_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "goal": goal,
            "coach_name": coach,
            "coach_phone": _generate_random_phone(),
            "gym_cost": cost,
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


# ===========================================================================
# Category 4: Long-Horizon Interaction with Exception Handling
# ===========================================================================


class TravelItineraryManagement(task_eval.TaskEval):
    """Task: Multi-day trip - schedule cities, map locations, itinerary note, contacts, SMS, budget."""

    app_names = ("simple calendar pro", "markor",
                 "simple contacts pro", "simple sms messenger", "pro expense")
    complexity = 7.5
    schema = {
        "type": "object",
        "properties": {
            "city1": {"type": "string"},
            "city2": {"type": "string"},
            "city3": {"type": "string"},
            "date1": {"type": "string"},
            "date2": {"type": "string"},
            "date3": {"type": "string"},
            "phone": {"type": "string"},
            "budget": {"type": "number"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "city1", "city2", "city3",
            "date1", "date2", "date3",
            "phone", "budget", "note_file_name",
        ],
    }
    template = (
        "Plan a 3-day trip. 1) Create daily events in Simple Calendar Pro: "
        "'Day 1: {city1}' on {date1}, 'Day 2: {city2}' on {date2}, "
        "'Day 3: {city3}' on {date3}. 2) Create a travel itinerary note in "
        "Markor named {note_file_name} listing all cities and dates. "
        "3) Add a travel companion contact in Simple Contacts Pro. "
        "4) Send the full itinerary via SMS to {phone}. "
        "5) Add trip budget ${budget} in Pro Expense."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        cal = _check_calendar_has_event(env, keyword="Day 1")
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["city1"], self.params["city2"],
             self.params["city3"]],
        )
        sms = _check_sms_sent(
            env, self.params["phone"], self.params["city1"], time_mins=60,
        )
        if not cal or not note or not sms:
            print(f"\n[FAIL DETAILS] calendar={cal}, note={note}, sms={sms}")
        return (cal + note + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        cities = [
            user_data_generation.generate_random_string(8).title(),
            user_data_generation.generate_random_string(8).title(),
            user_data_generation.generate_random_string(8).title(),
        ]
        dates = [_generate_random_date() for _ in range(3)]
        budget = round(random.uniform(500, 3000), 2)
        note_name = f"travel_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "city1": cities[0], "city2": cities[1], "city3": cities[2],
            "date1": dates[0]["date_str"],
            "date2": dates[1]["date_str"],
            "date3": dates[2]["date_str"],
            "phone": _generate_random_phone(),
            "budget": budget,
            "note_file_name": note_name,
        }


class EventPlanningAndCoordination(task_eval.TaskEval):
    """Task: Community event planning - calendar, participants, invitations, note, budget."""

    app_names = ("simple calendar pro", "simple contacts pro",
                 "simple sms messenger", "markor", "pro expense")
    complexity = 8.0
    schema = {
        "type": "object",
        "properties": {
            "event_date": {"type": "string"},
            "participant_name": {"type": "string"},
            "participant_phone": {"type": "string"},
            "venue_cost": {"type": "number"},
            "catering_cost": {"type": "number"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "event_date", "participant_name", "participant_phone",
            "venue_cost", "catering_cost", "phone", "note_file_name",
        ],
    }
    template = (
        "You are organizing a community event. 1) Create 'Community Event' in "
        "Simple Calendar Pro on {event_date}. 2) Add participant contact "
        "'{participant_name}' with phone {participant_phone} in Simple Contacts "
        "Pro. 3) Send SMS invitation to {participant_phone}. 4) Create event "
        "planning note in Markor named {note_file_name} listing participants "
        "and agenda. 5) Add expenses in Pro Expense: venue ${venue_cost}, "
        "catering ${catering_cost}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        cal = _check_calendar_has_event(env, keyword="Community Event")
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["participant_name"], "Community Event"],
        )
        sms = _check_sms_sent(
            env, self.params["participant_phone"], "invited", time_mins=60,
        )
        if not cal or not note or not sms:
            print(f"\n[FAIL DETAILS] calendar={cal}, note={note}, sms={sms}")
        return (cal + note + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        participant = user_data_generation.generate_random_string(7).title()
        venue = round(random.uniform(100, 500), 2)
        catering = round(random.uniform(50, 300), 2)
        note_name = f"event_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "event_date": d["date_str"],
            "participant_name": participant,
            "participant_phone": _generate_random_phone(),
            "venue_cost": venue,
            "catering_cost": catering,
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class ProjectKickoffAndTeamSetup(task_eval.TaskEval):
    """Task: Project kickoff - schedule meetings, add team contacts, send messages, note, budget."""

    app_names = ("simple calendar pro", "simple contacts pro",
                 "simple sms messenger", "markor", "pro expense")
    complexity = 8.0
    schema = {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "kickoff_date": {"type": "string"},
            "deadline_date": {"type": "string"},
            "member_name": {"type": "string"},
            "member_phone": {"type": "string"},
            "budget": {"type": "number"},
            "phone": {"type": "string"},
            "note_file_name": {"type": "string"},
        },
        "required": [
            "project_name", "kickoff_date", "deadline_date",
            "member_name", "member_phone", "budget",
            "phone", "note_file_name",
        ],
    }
    template = (
        "You are starting a new project '{project_name}'. 1) Create project "
        "schedule in Simple Calendar Pro: kickoff meeting on {kickoff_date}, "
        "deadline on {deadline_date}. 2) Add team member contact '{member_name}' "
        "with phone {member_phone} in Simple Contacts Pro. 3) Send welcome SMS "
        "to {member_phone} about the project. 4) Create project note in Markor "
        "named {note_file_name} with goals and team info. 5) Add project budget "
        "${budget} in Pro Expense."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        cal = _check_calendar_has_event(env, keyword="meeting")
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["project_name"], self.params["member_name"]],
        )
        sms = _check_sms_sent(
            env, self.params["member_phone"], "project", time_mins=60,
        )
        if not cal or not note or not sms:
            print(f"\n[FAIL DETAILS] calendar={cal}, note={note}, sms={sms}")
        return (cal + note + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d1 = _generate_random_date()
        d2 = _generate_random_date()
        project = user_data_generation.generate_random_string(10).title()
        member = user_data_generation.generate_random_string(7).title()
        budget = round(random.uniform(1000, 10000), 2)
        note_name = f"project_{project.lower().replace(' ', '_')}_{user_data_generation.generate_random_string(4)}.txt"
        return {
            "project_name": project,
            "kickoff_date": d1["date_str"],
            "deadline_date": d2["date_str"],
            "member_name": member,
            "member_phone": _generate_random_phone(),
            "budget": budget,
            "phone": _generate_random_phone(),
            "note_file_name": note_name,
        }


class FamilyHealthRecordSetup(task_eval.TaskEval):
    """Task: Family health records - contacts, health note, appointments, expenses, SMS."""

    app_names = ("simple contacts pro", "markor",
                 "simple calendar pro", "pro expense", "simple sms messenger")
    complexity = 8.5
    schema = {
        "type": "object",
        "properties": {
            "spouse_name": {"type": "string"},
            "spouse_phone": {"type": "string"},
            "child_name": {"type": "string"},
            "doctor_name": {"type": "string"},
            "doctor_phone": {"type": "string"},
            "date1": {"type": "string"},
            "date2": {"type": "string"},
            "cost1": {"type": "number"},
            "cost2": {"type": "number"},
            "note_file_name": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": [
            "spouse_name", "spouse_phone", "child_name",
            "doctor_name", "doctor_phone",
            "date1", "date2", "cost1", "cost2",
            "note_file_name", "phone",
        ],
    }
    template = (
        "Set up family health records. 1) Add family contacts in Simple Contacts "
        "Pro: spouse '{spouse_name}' ({spouse_phone}), child '{child_name}', "
        "doctor '{doctor_name}' ({doctor_phone}). 2) Create a family health note "
        "in Markor named {note_file_name} with health info for each member. "
        "3) Schedule checkup appointments in Simple Calendar Pro: {spouse_name} "
        "on {date1}, {child_name} on {date2}. 4) Add medical expenses in Pro "
        "Expense: {spouse_name} ${cost1}, {child_name} ${cost2}. 5) Send health "
        "summary via SMS to {spouse_phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["spouse_name"], self.params["child_name"]],
        )
        cal = _check_calendar_has_event(env, keyword="Checkup")
        sms = _check_sms_sent(
            env, self.params["spouse_phone"], "health", time_mins=60,
        )
        if not note or not cal or not sms:
            print(f"\n[FAIL DETAILS] note={note}, calendar={cal}, sms={sms}")
        return (note + cal + sms) / 3.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        spouse = user_data_generation.generate_random_string(7).title()
        child = user_data_generation.generate_random_string(6).title()
        doctor = user_data_generation.generate_random_string(7).title()
        d1 = _generate_random_date()
        d2 = _generate_random_date()
        c1 = round(random.uniform(50, 300), 2)
        c2 = round(random.uniform(50, 300), 2)
        note_name = f"family_health_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "spouse_name": spouse,
            "spouse_phone": _generate_random_phone(),
            "child_name": child,
            "doctor_name": doctor,
            "doctor_phone": _generate_random_phone(),
            "date1": d1["date_str"],
            "date2": d2["date_str"],
            "cost1": c1, "cost2": c2,
            "note_file_name": note_name,
            "phone": _generate_random_phone(),
        }


class SmallBusinessDailyOperations(task_eval.TaskEval):
    """Task: Small business daily ops - sales log, expenses, schedule, customer contacts, SMS."""

    app_names = ("markor", "pro expense",
                 "simple calendar pro", "simple contacts pro",
                 "simple sms messenger")
    complexity = 9.0
    schema = {
        "type": "object",
        "properties": {
            "item1": {"type": "string"},
            "item2": {"type": "string"},
            "item3": {"type": "string"},
            "price1": {"type": "number"},
            "price2": {"type": "number"},
            "price3": {"type": "number"},
            "supply_cost": {"type": "number"},
            "utility_cost": {"type": "number"},
            "customer_name": {"type": "string"},
            "customer_phone": {"type": "string"},
            "partner_phone": {"type": "string"},
            "note_file_name": {"type": "string"},
            "date": {"type": "string"},
        },
        "required": [
            "item1", "item2", "item3",
            "price1", "price2", "price3",
            "supply_cost", "utility_cost",
            "customer_name", "customer_phone",
            "partner_phone", "note_file_name", "date",
        ],
    }
    template = (
        "Complete today's shop operations. 1) Create a daily sales log in Markor "
        "named {note_file_name} recording sales: {item1} ${price1}, {item2} "
        "${price2}, {item3} ${price3}. 2) Add expenses in Pro Expense: supplies "
        "${supply_cost}, utilities ${utility_cost}. 3) Create tomorrow's work "
        "schedule in Simple Calendar Pro. 4) Add customer contact '{customer_name}' "
        "({customer_phone}) in Simple Contacts Pro. 5) Send thank-you SMS to "
        "{customer_phone}. 6) Send daily sales summary via SMS to partner "
        "{partner_phone}."
    )

    def initialize_task(self, env: interface.AsyncEnv) -> None:
        super().initialize_task(env)
        _clear_task_app_data(self.app_names, env)
        adb_utils.clear_app_data("com.simplemobiletools.calendar.pro", env.controller)

    def is_successful(self, env: interface.AsyncEnv) -> float:
        super().is_successful(env)
        note = _check_file_contains(
            env, self.params["note_file_name"],
            device_constants.MARKOR_DATA,
            [self.params["item1"], self.params["item2"],
             self.params["item3"]],
        )
        sms1 = _check_sms_sent(
            env, self.params["customer_phone"], "thank", time_mins=60,
        )
        sms2 = _check_sms_sent(
            env, self.params["partner_phone"], self.params["item1"], time_mins=60,
        )
        cal = _check_calendar_has_event(env, keyword="Open Shop")
        if not note or not sms1 or not sms2 or not cal:
            print(f"\n[FAIL DETAILS] note={note}, sms1={sms1}, "
                  f"sms2={sms2}, calendar={cal}")
        return (note + sms1 + sms2 + cal) / 4.0

    def tear_down(self, env: interface.AsyncEnv) -> None:
        super().tear_down(env)
        _clear_task_app_data(self.app_names, env)

    @classmethod
    def generate_random_params(cls) -> dict[str, Any]:
        d = _generate_random_date()
        items = [
            user_data_generation.generate_random_string(7).title(),
            user_data_generation.generate_random_string(7).title(),
            user_data_generation.generate_random_string(7).title(),
        ]
        prices = [round(random.uniform(10, 100), 2) for _ in range(3)]
        supply = round(random.uniform(50, 200), 2)
        utility = round(random.uniform(20, 80), 2)
        customer = user_data_generation.generate_random_string(7).title()
        note_name = f"sales_{user_data_generation.generate_random_string(6)}.txt"
        return {
            "item1": items[0], "item2": items[1], "item3": items[2],
            "price1": prices[0], "price2": prices[1], "price3": prices[2],
            "supply_cost": supply, "utility_cost": utility,
            "customer_name": customer,
            "customer_phone": _generate_random_phone(),
            "partner_phone": _generate_random_phone(),
            "note_file_name": note_name,
            "date": d["date_str"],
        }


# ---------------------------------------------------------------------------
# Helper: Check calendar events via SQLite
# ---------------------------------------------------------------------------

def _check_calendar_has_event(env, keyword: str) -> bool:
    """Check if the calendar has an event matching the keyword.

    Uses the original AndroidWorld sqlite_utils to pull and query the DB,
    which handles permissions and DB structure more robustly.
    """
    try:
        from android_world.task_evals.single.calendar import calendar_utils
        from android_world.task_evals.utils import sqlite_schema_utils
        from android_world.task_evals.utils import sqlite_utils

        rows = sqlite_utils.get_rows_from_remote_device(
            calendar_utils.EVENTS_TABLE,
            calendar_utils.DB_PATH,
            sqlite_schema_utils.CalendarEvent,
            env,
        )

        for row in rows:
            if hasattr(row, "title") and row.title:
                if keyword.lower() in row.title.lower():
                    print(f"[CALENDAR CHECK] Found matching event: {row.title}")
                    return True
        print(f"[CALENDAR CHECK] No matching event found in {len(rows)} rows")
        return False
    except Exception as e:
        print(f"[CALENDAR CHECK] Failed using sqlite_utils: {e}")
        return False
