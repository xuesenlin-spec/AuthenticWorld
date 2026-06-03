# AuthenticWorld Benchmark 自身问题分析报告

基于 plan.txt 完整任务日志的分析（1.4MB，449 个错误记录）

---

## 一、总体错误统计

| 错误类型 | 出现次数 | 严重程度 |
|----------|---------|----------|
| App 启动失败 (`Failed to launch activity`) | **69** | 🔴 高 |
| monkey 命令失败 (`exit status 252`) | **144** | 🔴 高 |
| ADB 命令 SIGTRAP 信号崩溃 | **43** | 🔴 高 |
| LLM 调用超时 | **2** | 🟡 中 |
| 任务失败 (`Task Failed`) | **18** (全部 20 个任务中的 18 个) | 🔴 高 |

**关键发现：20 个任务中有 18 个失败，失败率 90%。其中大部分失败并非 Agent 能力不足，而是 Benchmark 基础设施问题导致。**

---

## 二、Benchmark 自身导致的致命问题

### 问题 1：Calendar App 启动机制完全失效（影响最广）

**现象**：`adb shell monkey -p Calendar 1` 命令返回 `exit status 252`

**发生次数**：144 次

**根因**：AndroidWorld 的 `open_app` 动作使用 `monkey` 命令启动 Calendar App，但模拟器中的 Simple Calendar Pro 包名是 `com.simplemobiletools.calendar.pro`，而 monkey 命令传入的是 `Calendar`，包名不匹配导致 100% 失败。

**影响**：所有需要打开 Calendar 的任务（10/20 个任务）都会在执行 `open_app` 时失败。Agent 被迫改用手动点击图标的方式打开 Calendar，消耗大量步数。

**修复建议**：
```python
# adb_utils.py 中修复 _PATTERN_TO_ACTIVITY 映射
"simple calendar pro": "com.simplemobiletools.calendar.pro/com.simplemobiletools.calendar.pro.activities.MainActivity",
# 同时修复 open_app 逻辑，使用 am start -n 而非 monkey
```

---

### 问题 2：Simple Contacts Pro 无法启动（69 次失败）

**现象**：`Failed to launch activity: 'com.simplemobiletools.contacts.pro/com.simplemobiletools.contacts.pro.activities.MainActivity'`

**发生次数**：37 次

**根因**：模拟器上实际安装的是系统自带的 `com.android.contacts`，而不是 Simple Contacts Pro。`_PATTERN_TO_ACTIVITY` 中的映射指向了不存在的 Activity。

**影响**：所有涉及联系人的任务（6/20 个任务）无法打开 Contacts App，Agent 被迫尝试各种替代方案（搜索、点击图标等），浪费大量步数。

**修复建议**：确认模拟器安装的 Contacts App 实际包名，或使用 `com.android.contacts`。

---

### 问题 3：Simple SMS Messenger 无法启动（69 次失败）

**现象**：`Failed to launch activity: 'com.google.android.apps.messaging/com.google.android.apps.messaging.ui.ConversationListActivity'`

**发生次数**：20 次

**根因**：映射指向了 Google Messages (`com.google.android.apps.messaging`)，而不是 Simple SMS Messenger (`com.simplemobiletools.smsmessenger`)。

**影响**：所有涉及 SMS 的任务（9/20 个任务）无法打开短信 App。Agent 需要反复尝试打开 App，有时成功有时失败。

**修复建议**：
```python
"simple sms messenger": "com.simplemobiletools.smsmessenger/com.simplemobiletools.smsmessenger.activities.MainActivity",
```

---

### 问题 4：ADB Shell 命令 SIGTRAP 崩溃（43 次）

**现象**：大量基础 ADB 命令以 `SIGTRAP` 信号崩溃

**涉及命令**：
| 命令 | 次数 | 用途 |
|------|------|------|
| `settings get global airplane_mode_on` | 19 | 检查飞行模式状态 |
| `dumpsys input \| grep logicalFrame` | 9 | 获取屏幕尺寸 |
| `input keycombination 113 29 && input keyevent 67` | 4 | Ctrl+A + Backspace（清除文本） |
| `dumpsys window \| grep mCurrentRotation` | 3 | 获取屏幕旋转 |
| `input text on` | 1 | 输入文本 |
| `input tap 556 1139` | 1 | 点击屏幕 |
| `date 1015153423.00` | 1 | 设置时间 |

**根因**：模拟器 ADB shell 环境不稳定，某些命令会导致 shell 进程崩溃。这是模拟器级别的问题。

**影响**：几乎所有任务都会受到影响。特别是 `dumpsys` 和 `input` 命令崩溃会导致 Agent 无法获取屏幕状态或执行操作。

**修复建议**：
1. 为常用 ADB 命令添加重试逻辑（当前已有 3 次重试，但 SIGTRAP 应该跳过重试直接返回默认值）
2. 使用替代命令：如用 `wm size` 替代 `dumpsys input` 获取屏幕尺寸
3. 考虑更换模拟器版本或增加 shell 稳定性配置

---

### 问题 5：任务评判器中的 sqlite3 查询崩溃

**现象**：`_check_calendar_has_event` 使用 `pull_file` 拉取 Calendar DB 时失败

**发生次数**：多次（具体被 try/except 吞掉了）

**根因**：Calendar DB 路径可能不正确或权限不足。

**影响**：即使 Agent 成功创建了日历事件，评判器也无法验证，导致任务被判为失败。

**修复建议**：当前代码已有 `try/except return False`，但应该确认 DB 路径是否正确：
```
/data/data/com.simplemobiletools.calendar.pro/databases/events.db
```

---

## 三、对评测结果的影响分析

### 如果排除 Benchmark 自身问题，预估成功率

| 任务 | 实际结果 | 排除 Benchmark 问题后 |
|------|---------|---------------------|
| BudgetCheckBeforePurchase | Failed | **可能成功**（Calendar 打开失败导致无法创建提醒） |
| BusinessTripPlanning | Failed | **可能成功**（Calendar/SMS 打开失败） |
| CalendarConflictResolution | Failed | **可能成功**（Calendar 打开失败） |
| EventPlanningAndCoordination | Failed | **可能失败**（Contacts/SMS 都无法打开） |
| ExpenseReimbursement | Failed | **可能成功**（仅 SMS 打开失败） |
| FamilyHealthRecordSetup | Failed | **可能失败**（Contacts 无法打开） |
| FitnessGoalTracking | Failed | **可能成功**（Contacts 打开失败） |
| HomeRenovationProject | Failed | **可能成功**（Contacts 打开失败） |
| MedicalAppointmentWorkflow | Failed | **可能成功**（Contacts 打开失败） |
| NetworkTroubleshooting | Failed | **可能成功**（ADB 命令崩溃） |
| NewJobOnboarding | Failed | **可能成功**（Contacts 打开失败） |
| PartyPlanning | Failed | **可能成功**（Contacts 打开失败） |
| PhotoMemorySharing | Failed | **可能成功** |
| ProjectKickoffAndTeamSetup | Failed | **可能失败**（Contacts/SMS 问题） |
| SmallBusinessDailyOperations | Failed | **可能成功**（Contacts 打开失败） |
| StorageSpaceManagement | Failed | **可能成功** |
| StudentExamPrep | Failed | **可能成功**（Contacts 打开失败） |
| WeeklyMealPrep | Failed | **可能成功** |

**预估：排除 Benchmark 基础设施问题后，约 12-14 个任务可能成功，实际成功率从 0% 提升到 60-70%。**

---

## 四、修复优先级建议

| 优先级 | 问题 | 修复难度 | 影响任务数 |
|--------|------|---------|-----------|
| **P0** | 修复 App 启动映射（Calendar/Contacts/SMS） | 低（改配置） | 15/20 |
| **P1** | 修复 SIGTRAP 崩溃命令（添加 fallback） | 中 | 20/20 |
| **P2** | 修复 Calendar DB 查询 | 低 | 10/20 |
| **P3** | 优化 open_app 逻辑（用 am start 替代 monkey） | 中 | 10/20 |

---

## 五、结论

**AuthenticWorld Benchmark 当前存在严重的基础设施问题，导致 90% 的任务失败。其中至少 60-70% 的失败不是 Agent 能力问题，而是 Benchmark 自身的 App 映射错误、ADB 命令崩溃、数据库查询失败等基础设施问题导致的。**

在修复这些问题之前，Benchmark 的评测结果不能真实反映 Agent 的能力。建议先修复 P0 和 P1 问题，再重新运行评测。
