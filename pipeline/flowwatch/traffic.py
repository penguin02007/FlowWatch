"""Time-varying traffic model: turns conversations into bits-per-second samples."""
from __future__ import annotations

import os
import random
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .topology import (
    BASELINE,
    EXPORTERS_BY_NAME,
    HISTORY_INCIDENTS,
    MBPS,
    SCENARIOS,
    Conversation,
)

SITE_TZ = ZoneInfo(os.getenv("SITE_TZ", "UTC"))

# Weekday business-hours curve (site local time), one value per hour.
_BUSINESS = [
    0.06, 0.05, 0.05, 0.05, 0.05, 0.07, 0.15, 0.40, 0.80, 0.95, 1.00, 0.95,
    0.80, 0.90, 1.00, 0.95, 0.85, 0.55, 0.30, 0.18, 0.12, 0.10, 0.08, 0.07,
]
_WEEKEND_FACTOR = 0.15
_CAP_HEADROOM = 0.97


@dataclass
class FlowSample:
    exporter: str
    in_if: int
    out_if: int
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    transport: str
    bps: float


def _profile(conv: Conversation, local: datetime) -> float:
    if conv.profile == "flat":
        return 1.0
    if conv.profile == "nightly":
        return 1.0 if 1 <= local.hour < 3 else 0.0
    h = local.hour + local.minute / 60
    lo = _BUSINESS[int(h) % 24]
    hi = _BUSINESS[(int(h) + 1) % 24]
    value = lo + (hi - lo) * (h - int(h))
    return value * _WEEKEND_FACTOR if local.weekday() >= 5 else value


def _growth(conv: Conversation, t: datetime, anchor: datetime) -> float:
    """Growth applies to history only: traffic reaches today's level at the anchor."""
    if not conv.growth_per_day:
        return 1.0
    days = min(0.0, (t - anchor).total_seconds() / 86400)
    return (1 + conv.growth_per_day) ** days


def ephemeral_port(conv: Conversation) -> int:
    return 49152 + zlib.crc32(f"{conv.client}-{conv.server}-{conv.port}".encode()) % 16000


def anchor_for(now: datetime) -> datetime:
    """Midnight (site time) of the given day: history incidents are relative to it."""
    local = now.astimezone(SITE_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def history_scenarios(t: datetime, anchor: datetime) -> list[tuple[str, float]]:
    active = []
    for inc in HISTORY_INCIDENTS:
        day = anchor - timedelta(days=inc.days_ago)
        start = day.replace(hour=inc.hour, minute=inc.minute)
        if start <= t < start + timedelta(minutes=inc.duration_min):
            active.append((inc.scenario, inc.scale))
    return active


def samples_at(
    t: datetime,
    anchor: datetime,
    scenarios: list[tuple[str, float]] = (),
    rng: random.Random | None = None,
    noise: float = 0.08,
) -> list[FlowSample]:
    """Unidirectional flow samples (in bps) for instant ``t``, capped at link capacity."""
    rng = rng or random
    local = t.astimezone(SITE_TZ)
    demand: list[tuple[Conversation, float]] = [
        (c, _profile(c, local) * _growth(c, t, anchor)) for c in BASELINE
    ]
    for name, scale in scenarios:
        demand.extend((c, scale) for c in SCENARIOS[name].conversations)

    samples: list[FlowSample] = []
    for conv, factor in demand:
        if factor <= 0:
            continue
        eph = ephemeral_port(conv)
        for src, dst, sport, dport, in_if, out_if, mbps in (
            (conv.client, conv.server, eph, conv.port, conv.client_if, conv.server_if, conv.up_mbps),
            (conv.server, conv.client, conv.port, eph, conv.server_if, conv.client_if, conv.down_mbps),
        ):
            jitter = max(0.6, min(1.4, rng.gauss(1.0, noise)))
            bps = mbps * MBPS * factor * jitter
            if bps > 0:
                samples.append(
                    FlowSample(conv.exporter, in_if, out_if, src, dst, sport, dport, conv.transport, bps)
                )
    _apply_capacity(samples)
    return samples


def _apply_capacity(samples: list[FlowSample]) -> None:
    """A link cannot carry more than its capacity: squeeze flows proportionally."""
    load: dict[tuple[str, str, int], float] = defaultdict(float)
    for s in samples:
        load[(s.exporter, "in", s.in_if)] += s.bps
        load[(s.exporter, "out", s.out_if)] += s.bps
    factors: dict[tuple[str, str, int], float] = {}
    for key, bps in load.items():
        iface = EXPORTERS_BY_NAME[key[0]].interface(key[2])
        limit = iface.capacity_bps * _CAP_HEADROOM if iface else float("inf")
        factors[key] = min(1.0, limit / bps) if bps else 1.0
    for s in samples:
        s.bps *= min(factors[(s.exporter, "in", s.in_if)], factors[(s.exporter, "out", s.out_if)])
