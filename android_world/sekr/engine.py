import json
import os

class SEKREngine:
    """
    SEKR Engine: Self-Evolving Knowledge-Infused Reasoning Engine.
    Retrieves relevant guidance based on current context (History, Goal).
    """

    def __init__(self, kb_path=None):
        self.kb = []
        if kb_path is None:
            # Default path relative to this engine file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            kb_path = os.path.join(current_dir, 'knowledge.json')

        self.kb_path = kb_path
        if os.path.exists(self.kb_path):
            with open(self.kb_path, 'r') as f:
                data = json.load(f)
                self.kb = data.get("knowledge_base", [])

    def retrieve(self, history_text: str, goal_text: str) -> str:
        """
        Retrieve relevant knowledge based on keywords in history and goal.
        Returns a formatted string to be appended to the prompt.
        """
        relevant_tips = []
        context = (history_text + " " + goal_text).lower()

        # Simple Keyword Matching
        for item in self.kb:
            keywords = [k.lower() for k in item.get("keywords", [])]
            # Check if all keywords are present in context (stricter matching)
            # Or at least one? Let's use at least two keywords to avoid false positives.
            match_count = sum(1 for k in keywords if k in context)
            if match_count >= 1:
                relevant_tips.append(item["guidance"])

        if relevant_tips:
            # Avoid duplicates
            unique_tips = list(dict.fromkeys(relevant_tips))
            # Format with numbered checkboxes for SEKR compliance checking
            tips_formatted = "\n".join(
                f"[ ] Rule {i+1}: {tip}"
                for i, tip in enumerate(unique_tips)
            )
            return "\n\n[SEKR Knowledge Base]\n" + tips_formatted
        else:
            return ""

    def retrieve_with_raw_text(self, history_text: str, goal_text: str) -> tuple[str, list[dict]]:
        """
        Retrieve relevant knowledge and return both formatted string and raw entries.
        Returns (formatted_string, raw_entries_list).
        """
        relevant_tips = []
        relevant_entries = []
        context = (history_text + " " + goal_text).lower()

        for item in self.kb:
            keywords = [k.lower() for k in item.get("keywords", [])]
            match_count = sum(1 for k in keywords if k in context)
            if match_count >= 1:
                relevant_tips.append(item["guidance"])
                relevant_entries.append(item)

        if relevant_tips:
            unique_tips = list(dict.fromkeys(relevant_tips))
            unique_entries = list(
                {entry["guidance"]: entry for entry in relevant_entries}.values()
            )
            tips_formatted = "\n".join(
                f"[ ] Rule {i+1}: {tip}"
                for i, tip in enumerate(unique_tips)
            )
            return "\n\n[SEKR Knowledge Base]\n" + tips_formatted, unique_entries
        else:
            return "", []

    def evolve(self, new_entry: dict):
        """
        Add new knowledge to the database. (Used for Self-Evolution loop)
        """
        self.kb.append(new_entry)
        with open(self.kb_path, 'w') as f:
            json.dump({"knowledge_base": self.kb}, f, indent=2)
