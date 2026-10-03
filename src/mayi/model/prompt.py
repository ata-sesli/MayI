"""Auto input encoding: proposed call, user instructions, and observed history."""


def build_input(user_request, history, call):
    parts = [
        "### PROPOSED TOOL CALL",
        f"tool: {call['tool']}",
        f"args: {call['args']}",
        "",
        "### USER REQUEST",
        user_request if user_request else "(unavailable)",
        "",
        "### AGENT HISTORY",
    ]
    if history is None:
        parts.append("(unavailable)")
    elif not history:
        parts.append("(no prior actions)")
    else:
        for index, item in enumerate(history, 1):
            parts.append(
                f"[{index}] {item['tool']}({item['args']})\n-> {item.get('result', '')}"
            )
    return "\n".join(parts)
