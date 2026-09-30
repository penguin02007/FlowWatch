"""Function-calling loop: Gemini decides which Elasticsearch aggregations to run."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Callable

from google import genai
from google.genai import errors, types

from tools import SITE_TZ, TOOL_DECLARATIONS, FlowTools

MAX_TOOL_ROUNDS = 10

SYSTEM_PROMPT = """You are FlowWatch, a senior network operations analyst. You answer questions about
network performance and bandwidth using NetFlow v9 / IPFIX telemetry that is rolled up into an
Elasticsearch time series data stream. You can only see the data through the provided tools, each of
which runs Elasticsearch aggregations.

Current time: {now_local} ({tz}); UTC {now_utc}. Interpret relative dates ("yesterday afternoon",
"last Tuesday") in site time. Business hours are roughly 08:00-18:00 site time on weekdays.

How to work:
- Call get_network_inventory when you need interface names, capacities or application names. Never invent names.
- Ground every claim in tool results and quote concrete numbers: Mbps, % of link capacity, GB, times.
- Before calling something abnormal, compare it with a baseline (compare_to_baseline or detect_anomalies):
  traffic has strong time-of-day and weekday/weekend patterns.
- Correlate. When a link is congested, find which conversations, hosts and applications drove it
  (top_talkers filtered to that interface and window) and what else on the same link was squeezed.
- Prefer several focused tool calls to guessing. Use run_aggregation only when no other tool fits.
- Direction: on an interface, "inbound" is received from that link and "outbound" is sent into it.
  For the internet uplink, inbound means downloads and outbound means uploads.

Answer in markdown: a one-line bottom line first, then **Evidence** bullets with numbers and time
windows, then **Recommended next steps** (1-3 bullets). Keep it under about 200 words unless asked for
more detail. Mention the time zone once."""


def system_prompt() -> str:
    now = datetime.now(timezone.utc)
    return SYSTEM_PROMPT.format(
        now_local=now.astimezone(SITE_TZ).strftime("%A %Y-%m-%d %H:%M"),
        tz=str(SITE_TZ),
        now_utc=now.strftime("%Y-%m-%d %H:%M"),
    )


class NetOpsAgent:
    def __init__(self, tools: FlowTools, model: str | None = None):
        self.tools = tools
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.tool = types.Tool(function_declarations=[types.FunctionDeclaration(**d) for d in TOOL_DECLARATIONS])

    def _config(self, allow_tools: bool = True) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system_prompt(),
            tools=[self.tool],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode="AUTO" if allow_tools else "NONE")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )

    def _generate(self, history, config):
        """generate_content with backoff on rate limits and transient overloads.

        Free-tier keys allow only a few requests per minute, so waits add up to about a minute.
        """
        for delay in (5, 10, 20, 30, None):
            try:
                return self.client.models.generate_content(model=self.model, contents=history, config=config)
            except errors.APIError as err:
                if "PerDay" in str(err):
                    raise RuntimeError(
                        f"Daily Gemini quota exhausted for {self.model}. Use a paid API key or set "
                        "GEMINI_MODEL to another model (e.g. gemini-flash-lite-latest) in .env.") from err
                if err.code not in (429, 500, 503) or delay is None:
                    raise
                time.sleep(delay)

    def ask(self, history: list[types.Content], question: str,
            on_step: Callable[[dict], None] | None = None) -> tuple[str, list[dict]]:
        """Answer ``question``; ``history`` is updated in place so follow-ups keep context."""
        history.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
        steps: list[dict] = []
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            response = self._generate(history, self._config(round_no < MAX_TOOL_ROUNDS))
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None:
                reason = candidate.finish_reason if candidate else "no candidates"
                return f"The model returned no answer ({reason}).", steps
            history.append(candidate.content)
            parts = candidate.content.parts or []
            calls = [p.function_call for p in parts if p.function_call]
            if not calls:
                text = "".join(p.text for p in parts if p.text and not p.thought)
                return text or "(empty answer)", steps
            replies = []
            for call in calls:
                args = dict(call.args or {})
                result, trace = self.tools.call(call.name, args)
                step = {"tool": call.name, "args": args, "result": result, **trace}
                steps.append(step)
                if on_step:
                    on_step(step)
                replies.append(types.Part(function_response=types.FunctionResponse(
                    id=call.id, name=call.name, response={"result": result})))
            history.append(types.Content(role="user", parts=replies))
        return "Stopped: too many tool rounds.", steps
