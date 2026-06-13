"""Shared ReAct tool-calling protocol for providers without native function calling.

GitHub Copilot's thread API and the ChatGPT Codex backend don't expose OpenAI-style
function calling, so we teach the model — via the prompt — to emit a JSON tool-call
directive, intercept it in the stream, and surface it as a `tool_calls` StreamEvent so
the orchestrator's normal loop runs the Azure MCP tools and feeds results back.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.agent.provider import ToolCallRequest, ToolSpec


def compact_learn_result(result: dict[str, Any]) -> dict[str, Any]:
    """Shrink an Azure MCP tool 'learn' result for the model transcript.

    A `learn: true` call returns every sub-command of a service, each with a full
    parameter JSON schema — often 30-50KB. That bulk gets re-sent on every subsequent
    turn and blows GitHub Copilot's request-size limit. The model only needs each
    command's NAME, a one-line description, and its PARAMETER NAMES (with * for
    required) to construct the next call — not the verbose per-parameter schemas. This
    rewrites the result to keep exactly that, cutting size ~85% with no loss of the
    information needed to act. Non-learn results are returned unchanged.
    """
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result

    new_content: list[Any] = []
    changed = False
    for item in content:
        text = item if isinstance(item, str) else None
        if not text or "[" not in text or '"inputSchema"' not in text:
            new_content.append(item)
            continue
        start = text.find("[")
        try:
            arr = json.loads(text[start:])
        except (json.JSONDecodeError, ValueError):
            new_content.append(item)
            continue
        if not isinstance(arr, list):
            new_content.append(item)
            continue
        compact: list[dict[str, Any]] = []
        for c in arr:
            if not isinstance(c, dict):
                continue
            schema = c.get("inputSchema") or {}
            props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
            required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
            params = [f"{n}*" if n in required else n for n in props.keys()]
            compact.append(
                {
                    "name": c.get("name"),
                    "desc": (c.get("description") or "").splitlines()[0][:120],
                    "params": params,
                }
            )
        preamble = text[:start].strip()
        new_content.append((preamble + "\n" if preamble else "") + json.dumps(compact))
        changed = True

    if not changed:
        return result
    return {**result, "content": new_content}


def build_tool_instructions(tools: list[ToolSpec]) -> str:
    """A COMPACT, two-phase tool catalog + protocol instructions.

    The Azure MCP exposes ~60 service tools, each sharing the same calling shape
    (``intent``/``command``/``parameters``/``learn``) where the real operation lives in
    ``command``. Listing every service's full description on every turn is huge (~15KB)
    and blows past GitHub Copilot's request-size limit. So we use TWO-PHASE routing:

      Phase 1 — list services as a tiny directory (name + short blurb only) and state
                the shared "call with learn:true to see its commands" convention ONCE.
      Phase 2 — the model picks a service, calls it with ``learn: true`` to get that
                ONE service's commands, then calls it again with the chosen command.

    Flat (non-namespace) tools — a handful of direct entry points like
    ``subscription_list`` — are listed separately with their actual args.

    Framed as the *user's* helper tooling (not an identity override) to avoid the
    prompt-injection guardrails some hosted models apply.
    """
    _NAMESPACE_KEYS = {"intent", "command", "parameters", "learn"}

    def _props(t: ToolSpec) -> set[str]:
        return set(((t.parameters or {}).get("properties") or {}).keys())

    def _is_service(t: ToolSpec) -> bool:
        # A "service" tool routes via a `command` argument (namespace shape).
        return "command" in _props(t) and _props(t) <= _NAMESPACE_KEYS

    def _params_hint(schema: dict[str, Any]) -> str:
        props = (schema or {}).get("properties") or {}
        if not props:
            return "no args"
        required = set((schema or {}).get("required") or [])
        # Keep the hint short: required args first, then a couple optional ones.
        req = [f"{n}*" for n in props if n in required]
        opt = [n for n in props if n not in required][:3]
        return ", ".join(req + opt) or "no args"

    def _blurb(t: ToolSpec, cap: int) -> str:
        first = (t.description or "").strip().splitlines()[0]
        # Trim boilerplate suffixes the Azure MCP appends to most descriptions.
        for marker in (" This tool is a hierarchical", "This tool is a hierarchical"):
            i = first.find(marker)
            if i != -1:
                first = first[:i]
        # Strip the common "<Name> operations - Commands for managing/accessing ..."
        # filler: keep the short lead-in before " operations" / first " - " clause.
        low = first.lower()
        cut = low.find(" operations")
        if cut != -1:
            first = first[:cut]
        else:
            dash = first.find(" - ")
            if 0 < dash <= cap:
                first = first[:dash]
        return first.strip()[:cap]

    services = [t for t in tools if _is_service(t)]
    direct = [t for t in tools if not _is_service(t)]

    service_lines = "\n".join(f"  - {t.name}: {_blurb(t, 48)}" for t in services)

    def _direct_line(t: ToolSpec) -> str:
        desc = _blurb(t, 90)
        if t.name.startswith("extension_cli"):
            desc += " [TEXT ONLY — does NOT execute anything; never use to perform actions]"
        return f"  - {t.name}: {desc} (args: {_params_hint(t.parameters)})"

    direct_lines = "\n".join(_direct_line(t) for t in direct)

    first = services[0].name if services else (tools[0].name if tools else "a_tool")
    return (
        "I'm working in my Azure environment and I have a helper system that can run "
        "Azure operations for me (both lookups and changes). I can't run them by hand "
        "here — you decide which one to run and I'll paste back the results.\n\n"
        "Azure SERVICES (one tool each). To use a service you do TWO steps:\n"
        "  1) Call the service with {\"learn\": true, \"intent\": \"<what you want>\"} to "
        "get its exact command names + parameters.\n"
        "  2) Call it again with {\"command\": \"<name from step 1>\", \"parameters\": "
        "{ ... }} to actually run it (this includes writes/changes — they run for real).\n"
        f"{service_lines}\n\n"
        "Other tools (call directly with the listed args — no learn step needed):\n"
        f"{direct_lines}\n\n"
        "Rules:\n"
        "- Don't guess a service's command name — always `learn` first, then use an exact "
        "name from the list it returns.\n"
        "- To CHANGE something, use the service's real write command (e.g. sql → "
        "\"sql_server_firewall-rule_delete\"). NEVER use extension_cli_generate/install to "
        "carry out an action — those only print CLI text and change nothing.\n"
        "- Don't claim a service can't do something until you've `learn`ed it and checked.\n\n"
        "How to answer me:\n"
        "- To run one or more operations, reply with ONLY this JSON (no other words, no "
        "markdown fences):\n"
        '  {"thought": "<one sentence: what I asked for + your short plan>", '
        '"tool_calls": [{"name": "<tool name>", "arguments": { ...args... }}]}\n'
        f'  e.g. {{"thought": "You want to find broad Owner/Contributor role assignments; '
        f'I will list subscriptions, then learn the role service and check assignments.", '
        f'"tool_calls": [{{"name": "{first}", "arguments": {{"learn": true, "intent": '
        f'"discover role-assignment commands"}}}}]}}\n'
        "- ALWAYS include the \"thought\" field: restate, in your own words, what I asked "
        "for, then briefly what you're about to do. One or two sentences.\n"
        "- I'll reply with lines starting 'Tool result:' containing the data.\n"
        "- Once you have what you need, give me your normal written analysis and next "
        "steps (plain text, no JSON).\n\n"
        "IMPORTANT: if you need any Azure data or change to answer (you almost always "
        "do), your VERY FIRST reply MUST be the JSON object above — do NOT reply with a "
        "plain-text plan or 'Understood…' message on its own, because I can only act on "
        "the JSON. Put your understanding and plan in the \"thought\" field, not as "
        "separate prose.\n"
        "Please start by telling me which operation to run (as the JSON above) if you "
        "need Azure data or a change to answer."
    )



# Appended AFTER the running transcript to force the model's next action and stop it
# from continuing the dialogue as a script (some models hallucinate a "User:" turn or
# write "continue" and stall instead of emitting the next tool-call JSON).
NEXT_STEP_CUE = (
    "\n\n--- your turn now ---\n"
    "Reply with EITHER:\n"
    "  (a) the next tool-call JSON (when you still need data — most of the time), OR\n"
    "  (b) your final written answer in plain text (only once you have enough data).\n"
    "Do NOT write 'User:' or 'Assistant:'. Do NOT simulate my reply. Do NOT ask me to "
    "'continue' or wait for confirmation — just run the next query yourself by emitting "
    "the JSON. After a 'Tool result:' you must either call another tool (JSON) or give "
    "the final answer; never stop with only a description of what you intend to do.\n"
    "There is NO interactive dialog, popup, approval, or accept/reject prompt for you to "
    "wait on — none exists. Never say you are 'waiting for a response to a dialog' or "
    "'waiting for approval'. Tools run as soon as you emit the JSON, so just emit it."
)


_TOOLCALL_RE = re.compile(r"\{.*\}", re.DOTALL)


def _loads_lenient(s: str) -> Any:
    """json.loads, but tolerant of raw control characters inside string literals.

    Some models (notably when they emit a *text* tool-call directive instead of a
    native function call) write multi-line string arguments — e.g. a KQL/Resource
    Graph query — with literal newlines/tabs. That is invalid JSON, so a plain
    json.loads raises and the tool call is silently lost (and the turn ends with an
    empty answer). We retry once, escaping raw newlines/returns/tabs that appear
    *inside* JSON strings, which recovers these directives. Returns None on failure.
    """
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    out: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ch == "\n":
            out.append("\\n")
        elif in_str and ch == "\r":
            out.append("\\r")
        elif in_str and ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    try:
        return json.loads("".join(out))
    except json.JSONDecodeError:
        return None


def _balanced_json_candidates(text: str) -> list[str]:
    """Return candidate JSON-object substrings: for each ``{`` in the text, the span up
    to its *balanced* close (string-aware). Scanning from EVERY brace — not just
    top-level ones — means that even when the outer wrapper is malformed (a stray extra
    ``}`` that closes the root early, orphaning the ``tool_calls`` array), the
    well-formed inner ``{"name": …, "arguments": {…}}`` call object is still a candidate.
    The greedy ``\\{.*\\}`` regex can't recover these. Caller tries candidates
    longest-first and keeps the first that parses to a tool-call directive."""
    candidates: list[str] = []
    n = len(text)
    open_positions = [i for i, ch in enumerate(text) if ch == "{"]
    # Bound the work on pathological inputs (many braces); directives are small.
    for i in open_positions[:200]:
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(i, n):
            ch = text[j]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        candidates.append(text[i : end + 1] if end != -1 else text[i:])
    # Try the largest spans first (prefer the full directive over a nested fragment).
    candidates.sort(key=len, reverse=True)
    return candidates


def parse_tool_calls(text: str) -> list[ToolCallRequest]:
    """Detect a tool-call JSON directive in the model's output. Returns [] if none."""
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    if "{" not in stripped:
        return []
    # Try balanced-brace candidates (robust to trailing junk / an extra or missing brace)
    # before falling back to the greedy regex span.
    obj: Any = None
    for cand in _balanced_json_candidates(stripped):
        parsed = _loads_lenient(cand)
        if isinstance(parsed, dict) and (
            parsed.get("tool_calls") or parsed.get("tool_call") or parsed.get("name")
        ):
            obj = parsed
            break
    if obj is None:
        match = _TOOLCALL_RE.search(stripped)
        if match:
            obj = _loads_lenient(match.group(0))
    if not isinstance(obj, dict):
        return []

    raw_calls: list[Any] = []
    if isinstance(obj.get("tool_calls"), list):
        raw_calls = obj["tool_calls"]
    elif isinstance(obj.get("tool_call"), dict):
        raw_calls = [obj["tool_call"]]
    elif obj.get("name"):
        raw_calls = [obj]
    else:
        return []

    calls: list[ToolCallRequest] = []
    for rc in raw_calls:
        if not isinstance(rc, dict):
            continue
        name = rc.get("name") or (rc.get("function") or {}).get("name")
        if not name:
            continue
        args = rc.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(
            ToolCallRequest(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)
        )
    return calls


def extract_thought(text: str) -> str:
    """Pull the model's "thought" (its restated understanding + plan) out of a
    tool-call JSON directive, if present. Returns "" when there is none."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    if "{" not in stripped:
        return ""
    match = _TOOLCALL_RE.search(stripped)
    if not match:
        return ""
    obj = _loads_lenient(match.group(0))
    if not isinstance(obj, dict):
        return ""
    for key in ("thought", "plan", "thinking", "understanding"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def thought_for_calls(text: str, calls: list[ToolCallRequest]) -> str:
    """Best-effort 'understanding + plan' line to show the user before tools run.

    Prefers an explicit `thought` field in the directive; otherwise falls back to the
    first tool call's `intent` argument (which the model uses to describe what it's
    looking for), so the user always sees a plain-language restatement of the ask.
    """
    thought = extract_thought(text)
    if thought:
        return thought
    for call in calls or []:
        intent = (call.arguments or {}).get("intent")
        if isinstance(intent, str) and intent.strip():
            return intent.strip()
    return ""


def _find_balanced_json(text: str, start: int) -> int:
    """Return the index just past a balanced {...} object starting at `start`
    (text[start] must be '{'), or -1 if it isn't balanced. String-aware."""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _is_strip_directive(text: str) -> bool:
    """Stricter than ``parse_tool_calls`` for the STRIP path: only treat JSON as tool
    plumbing when it carries an unambiguous directive signal (``tool_calls`` /
    ``tool_call``, or a ``name`` *with* ``arguments`` / ``function``). This prevents
    stripping legitimate JSON in an answer that merely has a ``name`` field — e.g. an
    Azure resource object the user actually asked to see.
    """
    if not parse_tool_calls(text):
        return False
    return any(k in text for k in ('"tool_calls"', '"tool_call"', '"arguments"', '"function"'))


def strip_react_artifacts(text: str) -> str:
    """Remove ReAct protocol leakage from an assistant's visible answer.

    Providers without native tool-calling (GitHub Copilot, Codex) drive tools via a
    JSON protocol. Some models echo that protocol — the `{"tool_calls": …}` directive
    and `Tool result: {…}` lines — into their final written answer. Those are internal
    plumbing and must never be shown to the user, so strip them out defensively.
    """
    if not text:
        return text
    if "{" not in text and "Tool result" not in text:
        # No JSON/tool-result artifacts, but a hallucinated dialogue turn may still leak.
        return _strip_simulated_turns(text).strip()

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Strip a "Tool result:" prefix and any JSON payload that follows it.
        if text.startswith("Tool result:", i):
            j = i + len("Tool result:")
            while j < n and text[j] in " \t":
                j += 1
            if j < n and text[j] == "{":
                end = _find_balanced_json(text, j)
                if end != -1:
                    i = end
                    continue
            # No JSON payload — drop just to end of line.
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue

        # Strip a ```json … ``` fence whose body is a tool-call directive.
        if text.startswith("```", i):
            fence_end = text.find("```", i + 3)
            if fence_end != -1:
                body = text[i + 3 : fence_end]
                if _is_strip_directive(body):
                    i = fence_end + 3
                    continue

        # Strip a bare {...} object that is a tool-call directive.
        if text[i] == "{":
            end = _find_balanced_json(text, i)
            if end != -1 and _is_strip_directive(text[i:end]):
                i = end
                continue

        out.append(text[i])
        i += 1

    # Collapse the blank lines/whitespace left where artifacts were removed.
    cleaned = "".join(out)
    # Defensive: a malformed or truncated tool-call directive that the balanced-JSON
    # parser above could not consume (e.g. the model's output was cut off mid-directive,
    # so the braces never balance). A `{"thought": …}` / `{"tool_calls": …}` opener is
    # always internal plumbing, never prose — cut from it to the end of the text.
    opener = _DIRECTIVE_OPENER_RE.search(cleaned)
    if opener is not None:
        cleaned = cleaned[: opener.start()]
    # Drop standalone tool-result echoes ("Found N data") the model sometimes repeats.
    cleaned = _TOOL_RESULT_ECHO_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = _strip_simulated_turns(cleaned).strip()
    # If the entire "answer" was a leaked directive (nothing real remains), surface a
    # safe fallback so the user never sees raw plumbing or a blank bubble.
    if not cleaned and _DIRECTIVE_OPENER_RE.search(text) is not None:
        return _DIRECTIVE_FALLBACK
    return cleaned


# A ReAct/Codex tool-call directive opener. These keys never appear in user-facing
# prose, so their presence marks the start of leaked plumbing.
_DIRECTIVE_OPENER_RE = re.compile(r'\{\s*"(?:thought|tool_calls)"\s*:')
# A standalone "Found N data" tool-result echo on its own line.
_TOOL_RESULT_ECHO_RE = re.compile(r"(?m)^[ \t]*Found \d+ data\.?[ \t]*$")
_DIRECTIVE_FALLBACK = (
    "I gathered the data but couldn't compose a final summary for this turn. "
    "Please ask me to try again, or rephrase the question."
)


# Lines where the model hallucinates the dialogue script (it sees a "User:"/"Assistant:"
# transcript and continues it instead of acting). These must never reach the user.
_SIM_TURN_RE = re.compile(
    r"\n+\s*(user|assistant|tool result|assistant \(tool call\))\s*:.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_simulated_turns(text: str) -> str:
    """Remove a trailing hallucinated dialogue turn (e.g. 'User: continue')."""
    return _SIM_TURN_RE.sub("", text).rstrip()


_PLAN_OPENERS = (
    "i understand",
    "i understood",
    "goal:",
    "understanding:",
    "you want",
    "you're asking",
    "you are asking",
    "here's my plan",
    "here is my plan",
)


def strip_plan_preamble(text: str) -> str:
    """Remove a leading "understanding + plan" framing from the final answer.

    The agent is asked to state its understanding and plan as pre-work "thinking"
    (shown in a separate progress panel), not in the final answer. Some models still
    repeat it at the top of their answer; strip it defensively. Conservative: only
    strips when the text clearly OPENS with such a preamble AND a real Markdown
    heading section follows (so we never remove actual findings).
    """
    if not text or not text.strip():
        return text
    stripped = text.lstrip()
    head = stripped[:600].lower().lstrip("*_# ").strip()
    if not head.startswith(_PLAN_OPENERS):
        return text
    if "plan" not in stripped[:600].lower():
        return text
    lines = stripped.split("\n")
    cut_idx: int | None = None
    for i, line in enumerate(lines):
        # First real section heading marks the start of the actual answer.
        if i > 0 and line.lstrip().startswith("#"):
            cut_idx = i
            break
    if cut_idx is None:
        return text
    remainder = "\n".join(lines[cut_idx:]).strip()
    return remainder if len(remainder) > 30 else text


def _content_to_text(content: Any) -> str:
    """Render message content (str or OpenAI multimodal list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                parts.append(item["text"])
            elif item.get("type") == "image_url":
                parts.append("[image attached]")
        return "\n".join(parts)
    return ""


def flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Collapse OpenAI-style messages into a single transcript string, including the
    assistant's prior tool calls and tool results (so a ReAct loop has full context)."""
    system_parts: list[str] = []
    convo: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        tool_calls = m.get("tool_calls")

        if role == "assistant" and tool_calls:
            calls = [
                {"name": name, "arguments": (c.get("function") or {}).get("arguments", "")}
                for c in tool_calls
                if (name := (c.get("function") or {}).get("name", ""))
            ]
            ctext = _content_to_text(content)
            if calls:
                prefix = f"{ctext.strip()}\n" if ctext.strip() else ""
                convo.append(
                    f"Assistant (tool call): {prefix}"
                    + json.dumps({"tool_calls": calls}, ensure_ascii=False)
                )
                continue
            # No valid (named) tool calls — fall through to render any plain content.

        ctext = _content_to_text(content)
        if not ctext.strip():
            continue
        if role == "system":
            system_parts.append(ctext.strip())
        elif role == "assistant":
            convo.append(f"Assistant: {ctext.strip()}")
        elif role == "tool":
            convo.append(f"Tool result: {ctext.strip()}")
        else:
            convo.append(f"User: {ctext.strip()}")

    blocks: list[str] = []
    if system_parts:
        blocks.append("\n\n".join(system_parts))
    if convo:
        blocks.append("\n\n".join(convo))
    return "\n\n".join(blocks).strip()


class ToolCallDetector:
    """Stateful helper: feed streamed text chunks; it streams plain prose through
    token-by-token but, the moment a JSON/code-fence tool directive begins ANYWHERE in
    the stream, it stops emitting and buffers the rest so the directive can be parsed
    into tool calls at ``finish()``.

    This matters because models (esp. gpt-5.x / Codex) routinely write a short preamble
    sentence — "I'll look this up." — and THEN the ``{"tool_calls": …}`` directive. An
    earlier version only buffered when the message *started* with ``{``; a leading
    preamble caused the whole directive to stream as prose and the tool call to be
    silently dropped (the agent then hallucinated an answer with no data). We now begin
    buffering at the first ``{`` / ``[`` / opening code fence.

    Usage:
        det = ToolCallDetector(tools_enabled=bool(tools))
        for chunk in stream:
            for tok in det.feed(chunk):
                yield token(tok)
        calls, leftover = det.finish()
        if calls: yield tool_calls(calls)
        elif leftover: yield token(leftover)
    """

    def __init__(self, tools_enabled: bool) -> None:
        self._tools_enabled = tools_enabled
        self._buffering = False
        self._buffer = ""

    @staticmethod
    def _directive_start(text: str) -> int | None:
        """Index where a tool-call directive could begin (first ``{``/``[`` or code
        fence), else None. Used to split prose from a trailing directive in a chunk."""
        candidates = [text.find("{"), text.find("["), text.find("```")]
        idxs = [i for i in candidates if i != -1]
        return min(idxs) if idxs else None

    def feed(self, text: str):
        if not text:
            return
        # When tools are disabled (final-answer / summary turns) the model can't call a
        # tool, so stream everything verbatim — never swallow JSON code blocks etc.
        if not self._tools_enabled:
            yield text
            return
        if self._buffering:
            self._buffer += text
            return
        # Scanning prose: stream until a directive could begin, then buffer from there.
        idx = self._directive_start(text)
        if idx is None:
            yield text
            return
        if idx > 0:
            yield text[:idx]
        self._buffering = True
        self._buffer = text[idx:]

    def finish(self) -> tuple[list[ToolCallRequest], str]:
        if self._buffering and self._buffer.strip():
            calls = parse_tool_calls(self._buffer)
            if calls:
                return calls, ""
            return [], self._buffer
        return [], ""

    @property
    def buffer(self) -> str:
        """The raw buffered text (a tool directive when one was detected)."""
        return self._buffer
