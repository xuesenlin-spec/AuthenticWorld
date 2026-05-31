#!/bin/bash
export OPENAI_API_KEY="sk-5b4bb958a3e44bda85b5cbcc5e96d2ca"
export OPENAI_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
echo "=== Env check ==="
echo "OPENAI_API_KEY=${OPENAI_API_KEY:0:15}..."
echo "OPENAI_BASE_URL=${OPENAI_BASE_URL}"
python -u run.py --suite_family=android_world --agent_name=t3a_gpt4 --tasks=ContactsAddContact
