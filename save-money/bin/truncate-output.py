#!/usr/bin/env python3
"""
cost-truncate-output PostToolUse hook

When a tool's text output exceeds the configured token threshold, replaces it
with a head+pointer+tail summary. The full output is saved to /tmp. This keeps
large outputs out of the main agent's context on every subsequent turn.

Iron rule: never truncate image output (would destroy base64 data).
Unknown/unrecognized tool output structures are passed through unchanged.

Protocol: see tools/cost-truncate-output/README.md
"""

import json
import os
import sys
import tempfile

CONFIG_PATH = os.path.expanduser("~/.claude/cost-opt.json")

# Token-to-character approximation used throughout this script.
# 1 token ≈ 4 characters (conservative average for mixed English/code text).
# Using a constant keeps the approximation explicit and easy to adjust.
CHARS_PER_TOKEN = 4

DEFAULT_THRESHOLD_TOKENS = 25_000

# Head / tail sizes in tokens; converted to chars at runtime.
HEAD_TOKENS = 5_000
TAIL_TOKENS = 3_000


def load_config():
    """
    Returns (enabled, threshold_tokens).
    Only explicit enabled=True activates the hook; missing / null / false → disabled.
    """
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        tt = data.get("text_truncate") or {}
        enabled = tt.get("enabled") is True
        threshold = tt.get("threshold_tokens", DEFAULT_THRESHOLD_TOKENS)
        if not isinstance(threshold, int) or threshold <= 0:
            threshold = DEFAULT_THRESHOLD_TOKENS
        return enabled, threshold
    except Exception:
        return False, DEFAULT_THRESHOLD_TOKENS


def is_image_response(tool_name, tool_response):
    """
    Returns True if the tool response contains image data.
    - Bash: tool_response is a dict; isImage field indicates a screenshot.
    - Read/other: tool_response may be a list of content blocks containing
      image blocks.
    """
    if isinstance(tool_response, dict):
        if tool_response.get("isImage"):
            return True
    if isinstance(tool_response, list):
        for block in tool_response:
            if isinstance(block, dict) and block.get("type") == "image":
                return True
    return False


def extract_text_and_builder(tool_name, tool_response):
    """
    Returns (text, builder) where:
      text    — the string to measure and potentially truncate
      builder — callable(truncated_text) → new tool_response value

    Returns (None, None) if we don't know how to safely handle this tool.
    """
    if tool_name == "Bash":
        if not isinstance(tool_response, dict):
            return None, None
        stdout = tool_response.get("stdout", "")
        if not isinstance(stdout, str):
            return None, None

        def bash_builder(new_text):
            result = dict(tool_response)
            result["stdout"] = new_text
            return result

        return stdout, bash_builder

    if tool_name == "Read":
        # tool_response is a plain string (line-numbered text content)
        if isinstance(tool_response, str):
            return tool_response, lambda t: t
        # Image file: list of content blocks — handled by is_image_response
        return None, None

    # All other tools: unknown structure, pass through
    return None, None


def save_to_tmp(tool_name, full_text):
    """Writes full_text to a unique /tmp file, returns the path."""
    fd, path = tempfile.mkstemp(prefix=f"cost-truncate-{tool_name}-", suffix=".txt", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(full_text)
    except Exception:
        os.close(fd)
        raise
    return path


def truncate(text, threshold_chars, tool_name):
    """
    Returns truncated text with head + pointer line + tail.
    Saves full content to /tmp and embeds the path in the pointer line.
    """
    tmp_path = save_to_tmp(tool_name, text)

    head_chars = HEAD_TOKENS * CHARS_PER_TOKEN
    tail_chars = TAIL_TOKENS * CHARS_PER_TOKEN

    head = text[:head_chars]
    tail = text[max(0, len(text) - tail_chars):]

    pointer = (
        f"\n[输出过大已截断（原始 {len(text)} 字符 ≈ {len(text)//CHARS_PER_TOKEN} token），"
        f"完整内容存于 {tmp_path}，需要某段时读该文件]\n"
    )

    return head + pointer + tail


def main():
    try:
        stdin_data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        enabled, threshold_tokens = load_config()
    except Exception:
        sys.exit(0)

    if not enabled:
        sys.exit(0)

    threshold_chars = threshold_tokens * CHARS_PER_TOKEN

    tool_name = stdin_data.get("tool_name", "")
    tool_response = stdin_data.get("tool_response")

    try:
        if is_image_response(tool_name, tool_response):
            sys.exit(0)

        text, builder = extract_text_and_builder(tool_name, tool_response)
        if text is None:
            sys.exit(0)

        if len(text) <= threshold_chars:
            sys.exit(0)

        truncated = truncate(text, threshold_chars, tool_name)
        new_response = builder(truncated)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": new_response,
            }
        }
        print(json.dumps(output))
    except Exception as e:
        print(f"cost-truncate-output: error processing {tool_name}: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
