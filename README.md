# AuthenticWorld

**AuthenticWorld** is a long-horizon benchmark for autonomous GUI agents on Android devices.

Building on the AndroidWorld infrastructure, AuthenticWorld introduces 20 new **real-world, long-horizon cross-app tasks** (50-90 steps each, spanning 3-6 applications) and a new **PegaAgent** with Fast-Path and reflection capabilities.

## Key Contributions

### 1. Pega Benchmark: 20 Long-Horizon Tasks

Unlike existing benchmarks with short, single-app tasks, AuthenticWorld provides 20 tasks grounded in realistic user scenarios, organized into four capability dimensions:

| Category | Tasks | Description |
|----------|-------|-------------|
| **Cross-App Information Flow** | 5 | Data must flow through multiple apps (e.g., Calendar → SMS → Expense → Markor) |
| **Conditional Decision-Making** | 5 | Agents must inspect state and branch logic (e.g., "if WiFi is off, turn it on") |
| **Multi-Subgoal Workflows** | 5 | High-level goals composed of interdependent sub-tasks across apps |
| **Long-Horizon + Exception Handling** | 5 | Extended sequences requiring resilience to dialogs, permissions, and state changes |

Each task requires 50-90 steps and involves 3-6 different applications, exposing failure modes that short-horizon benchmarks cannot reveal.

### 2. PegaAgent: A Novel Long-Horizon GUI Agent

PegaAgent extends the T3A agent with two key capabilities:

#### Fast-Path for Transient UI
System dialogs (permission requests, "set default app" prompts, etc.) are **transient** — they appear and disappear within 2-5 seconds. Traditional agents send screenshots to an LLM and wait ~20 seconds for a response, by which time the dialog is gone. PegaAgent detects transient UI via package names and keyword patterns, and responds within **< 1 second** via a rule-based fast-path, bypassing the LLM entirely.

#### Loop Detection + Reflection
When an agent repeats the same action multiple times without progress, PegaAgent detects the loop and triggers a **reflection** process: the LLM analyzes the execution history to identify the root cause and proposes a new strategy. This enables the agent to recover from failure modes rather than blindly repeating the same mistake until the step budget is exhausted.

## Task Suite

### Cross-App Information Flow

| Task | Apps Involved | Complexity |
|------|--------------|------------|
| BusinessTripPlanning | Calendar → OsmAnd → Markor → SMS | 6.0 |
| ExpenseReimbursement | Markor → Expense → Calendar → SMS | 5.5 |
| PartyPlanning | Calendar → Contacts → Markor → SMS | 7.0 |
| MedicalAppointmentWorkflow | Markor → Calendar → Contacts → SMS | 5.5 |
| PhotoMemorySharing | Gallery → Markor → Calendar → SMS | 6.0 |

### Conditional Decision-Making and Error Handling

| Task | Apps Involved | Complexity |
|------|--------------|------------|
| NetworkTroubleshooting | Settings → Markor → SMS → Calendar | 5.5 |
| CalendarConflictResolution | Calendar → Markor → SMS | 5.5 |
| StorageSpaceManagement | Markor → Gallery → SMS | 5.5 |
| MissedCallFollowUp | Phone → Contacts → SMS → Markor | 5.5 |
| BudgetCheckBeforePurchase | Expense → Markor → Calendar → SMS | 6.5 |

### Multi-Subgoal Workflows

| Task | Apps Involved | Complexity |
|------|--------------|------------|
| NewJobOnboarding | Contacts → Calendar → Markor → SMS | 6.5 |
| WeeklyMealPrep | Markor → Expense → Calendar → SMS | 7.0 |
| HomeRenovationProject | Markor → Calendar → Expense → Contacts → SMS | 7.5 |
| StudentExamPrep | Calendar → Markor → Contacts → SMS | 6.5 |
| FitnessGoalTracking | Markor → Calendar → Expense → Contacts → SMS | 7.0 |

### Long-Horizon Interaction with Exception Handling

| Task | Apps Involved | Complexity |
|------|--------------|------------|
| TravelItineraryManagement | Calendar → OsmAnd → Markor → Contacts → SMS → Expense | 7.5 |
| EventPlanningAndCoordination | Calendar → Contacts → SMS → Markor → Expense | 8.0 |
| ProjectKickoffAndTeamSetup | Calendar → Contacts → SMS → Markor → Expense | 8.0 |
| FamilyHealthRecordSetup | Contacts → Markor → Calendar → Expense → SMS | 8.5 |
| SmallBusinessDailyOperations | Markor → Expense → Calendar → Contacts → SMS | 9.0 |

## Installation

1. Set up the Android Emulator
   1. Download Android Studio [here](https://developer.android.com/studio)
   2. Create an AVD: **Pixel 6**, **Tiramisu API Level 33**

2. Launch the emulator with gRPC support:
   ```bash
   ~/Library/Android/sdk/emulator/emulator -avd <your_avd_name> -grpc 8554
   ```

3. Install dependencies:
   ```bash
   pip install -e .
   ```

## Usage

### Run with PegaAgent (Fast-Path + Reflection)
```bash
python -u run.py --suite_family=pega --agent_name=pega_gpt4
```

### Run with T3A (baseline)
```bash
python -u run.py --suite_family=pega --agent_name=t3a_gpt4
```

### Run specific tasks
```bash
python -u run.py --suite_family=pega --agent_name=pega_gpt4 --tasks=BusinessTripPlanning
```

### Available agents
| Agent Name | Description |
|------------|-------------|
| `pega_gpt4` | PegaAgent with Fast-Path and reflection (recommended) |
| `t3a_gpt4` | T3A text-only agent (baseline) |
| `m3a_gpt4v` | M3A multimodal agent with screenshots |

## Project Structure

```
android_world/
├── agents/
│   ├── pega_agent.py      # PegaAgent: Fast-Path + reflection
│   ├── t3a.py             # T3A baseline agent
│   └── ...
├── task_evals/
│   └── composite/
│       ├── pega.py        # 20 long-horizon tasks
│       └── calendar_sms_markor.py  # Original composite task
├── registry.py            # Task and suite family registry
```

## Citation

If you use AuthenticWorld in your research, please cite:

```bibtex
@misc{authenticworld2026,
  title={AuthenticWorld: A Long-Horizon Benchmark for Autonomous GUI Agents},
  author={PegaAgent Team},
  year={2026},
  url={https://github.com/xuesenlin-spec/AuthenticWorld}
}
```

## License

This project is based on [AndroidWorld](https://github.com/google-research/android_world) by Google Research, licensed under the Apache 2.0 License.
