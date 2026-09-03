from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .runs import LaneRun

# Tokens are exact, from the provider's usage object - never estimated. Cost is
# tokens x a price table, computed HERE at read time and never stored: prices
# change, and a stored dollar figure makes an old comparison silently
# inconsistent. Every result carries the `price_version` it was computed under.
#
# The bundled table below is illustrative. Swap `PriceTable` for a maintained
# pricing library; nothing else in analysis changes.


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens. Cached input and output are separate rates - a
    single blended rate is wrong on any model with prompt caching."""
    input_per_mtok: float
    cached_input_per_mtok: float
    output_per_mtok: float


@dataclass(frozen=True)
class PriceTable:
    version: str
    prices: Mapping[str, ModelPrice]

    def for_model(self, model: str | None) -> ModelPrice | None:
        if not model:
            return None
        if model in self.prices:
            return self.prices[model]
        # longest table key that is a prefix of the observed model string
        cand = [k for k in self.prices if model.startswith(k)]
        return self.prices[max(cand, key=len)] if cand else None


BUNDLED_PRICES = PriceTable(
    version="amc-bundled-2026-02-01",
    prices={
        "claude-opus-5":    ModelPrice(15.0, 1.5, 75.0),
        "claude-sonnet-5":  ModelPrice(3.0, 0.30, 15.0),
        "claude-haiku-4-5": ModelPrice(0.80, 0.08, 4.0),
        "gpt-4o-mini":      ModelPrice(0.15, 0.075, 0.60),
        "gpt-4o":           ModelPrice(2.5, 1.25, 10.0),
        "gemini-2.0-flash": ModelPrice(0.10, 0.025, 0.40),
    },
)


@dataclass(frozen=True)
class TokenTotals:
    lane_id: str
    tokens_in: int          # sum of reported prompt tokens (cached + uncached)
    tokens_out: int
    cached_tokens: int
    llm_calls: int
    missing_in: int         # model calls that reported no prompt-token count
    missing_out: int

    @property
    def complete(self) -> bool:
        return self.missing_in == 0 and self.missing_out == 0


def token_totals(lane: LaneRun) -> TokenTotals:
    tin = tout = cached = missing_in = missing_out = 0
    llm = lane.llm_events
    for e in llm:
        if e.tokens_in is None:
            missing_in += 1
        else:
            tin += e.tokens_in
        if e.tokens_out is None:
            missing_out += 1
        else:
            tout += e.tokens_out
        if e.cached_tokens is not None:
            cached += e.cached_tokens
    return TokenTotals(
        lane_id=lane.lane_id,
        tokens_in=tin, tokens_out=tout, cached_tokens=cached,
        llm_calls=len(llm), missing_in=missing_in, missing_out=missing_out,
    )


@dataclass(frozen=True)
class CostBreakdown:
    lane_id: str
    model: str | None
    price_version: str
    priced: bool                 # a rate was found for this model
    incomplete: bool             # some model call reported no token count
    input_cost: float | None
    cached_cost: float | None
    output_cost: float | None
    total_cost: float | None


def cost_for_lane(lane: LaneRun, price_table: PriceTable = BUNDLED_PRICES) -> CostBreakdown:
    totals = token_totals(lane)
    price = price_table.for_model(lane.model)

    if price is None:
        return CostBreakdown(
            lane_id=lane.lane_id, model=lane.model,
            price_version=price_table.version, priced=False,
            incomplete=not totals.complete,
            input_cost=None, cached_cost=None, output_cost=None, total_cost=None,
        )

    uncached_in = max(0, totals.tokens_in - totals.cached_tokens)
    input_cost = uncached_in / 1_000_000 * price.input_per_mtok
    cached_cost = totals.cached_tokens / 1_000_000 * price.cached_input_per_mtok
    output_cost = totals.tokens_out / 1_000_000 * price.output_per_mtok

    return CostBreakdown(
        lane_id=lane.lane_id, model=lane.model,
        price_version=price_table.version, priced=True,
        incomplete=not totals.complete,
        input_cost=input_cost, cached_cost=cached_cost, output_cost=output_cost,
        total_cost=input_cost + cached_cost + output_cost,
    )
