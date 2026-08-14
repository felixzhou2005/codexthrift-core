from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any

MICROUSD_PER_USD = 1_000_000
TOKENS_PER_MILLION = 1_000_000
LONG_CONTEXT_THRESHOLD = 272_000
PRICING_BASIS = "openai_api_standard"
PRICING_CATALOG_VERSION = "openai-standard-2026-08-11"
ESTIMATOR_VERSION = "v2"


class BillingMode(StrEnum):
    CHATGPT_SUBSCRIPTION_INCLUDED = "chatgpt_subscription_included"
    CHATGPT_CREDITS = "chatgpt_credits"
    OPENAI_API = "openai_api"
    THIRD_PARTY_API = "third_party_api"
    UNKNOWN = "unknown"


class ContextClass(StrEnum):
    SHORT = "short"
    LONG = "long"


class EstimateStatus(StrEnum):
    ESTIMATED_COMPLETE = "estimated_complete"
    USAGE_MISSING = "usage_missing"
    USAGE_INCONSISTENT = "usage_inconsistent"
    BILLING_MODEL_UNKNOWN = "billing_model_unknown"
    MODEL_UNPRICED = "model_unpriced"
    TOOL_PRICING_INCOMPLETE = "tool_pricing_incomplete"
    LEGACY_PARTIAL = "legacy_partial"


class ActualCostStatus(StrEnum):
    SUBSCRIPTION_INCLUDED = "subscription_included"
    PROVIDER_REPORTED = "provider_reported"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PricingRates:
    input_microusd_per_million: int
    cached_input_microusd_per_million: int
    cache_write_microusd_per_million: int
    output_microusd_per_million: int


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class BillingModelResolution:
    billing_model: str | None
    source: str | None


@dataclass(frozen=True)
class CostEstimate:
    status: EstimateStatus
    context_class: ContextClass | None
    regular_input_tokens: int | None
    regular_input_cost_microusd: int | None
    cached_input_cost_microusd: int | None
    cache_write_cost_microusd: int | None
    output_cost_microusd: int | None
    model_token_cost_microusd: int | None
    builtin_tool_cost_microusd: int | None
    storage_cost_microusd: int | None
    container_cost_microusd: int | None
    known_api_equivalent_cost_microusd: int | None
    api_equivalent_cost_microusd: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActualCostResult:
    status: ActualCostStatus
    amount_microusd: int | None


_SHORT_RATES = {
    "gpt-5.6-sol": PricingRates(5_000_000, 500_000, 6_250_000, 30_000_000),
    "gpt-5.6": PricingRates(5_000_000, 500_000, 6_250_000, 30_000_000),
    "gpt-5.6-terra": PricingRates(2_000_000, 200_000, 2_500_000, 12_000_000),
    "gpt-5.6-luna": PricingRates(200_000, 20_000, 250_000, 1_200_000),
}


def _long_rates(short: PricingRates) -> PricingRates:
    return PricingRates(
        input_microusd_per_million=short.input_microusd_per_million * 2,
        cached_input_microusd_per_million=short.cached_input_microusd_per_million * 2,
        cache_write_microusd_per_million=short.cache_write_microusd_per_million * 2,
        output_microusd_per_million=short.output_microusd_per_million * 3 // 2,
    )


STANDARD_API_PRICING: dict[tuple[str, ContextClass], PricingRates] = {
    (model, ContextClass.SHORT): rates
    for model, rates in _SHORT_RATES.items()
} | {
    (model, ContextClass.LONG): _long_rates(rates)
    for model, rates in _SHORT_RATES.items()
}


_ALIAS_TO_MODEL = {
    "luna-low": "gpt-5.6-luna",
    "luna-medium": "gpt-5.6-luna",
    "terra-medium": "gpt-5.6-terra",
    "sol-medium": "gpt-5.6-sol",
    "sol-high": "gpt-5.6-sol",
    "sol-xhigh": "gpt-5.6-sol",
    "reasoning-medium": "gpt-5.6-sol",
    "codex-auto-review": "gpt-5.6-sol",
}


def context_class_for(input_tokens: int) -> ContextClass:
    return ContextClass.LONG if input_tokens > LONG_CONTEXT_THRESHOLD else ContextClass.SHORT


def normalize_billing_model(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in _ALIAS_TO_MODEL:
        return _ALIAS_TO_MODEL[text]
    for model in ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6"):
        if text == model or text.endswith(f"/{model}"):
            return model
    return None


def resolve_billing_model(
    *,
    response_model: Any = None,
    provider_model: Any = None,
    selected_model: Any = None,
    selected_alias: Any = None,
    requested_alias: Any = None,
) -> BillingModelResolution:
    for source, value in (
        ("response_model", response_model),
        ("provider_model", provider_model),
        ("selected_model", selected_model),
        ("selected_alias", selected_alias),
        ("requested_alias", requested_alias),
    ):
        model = normalize_billing_model(value)
        if model is not None:
            return BillingModelResolution(model, source)
    return BillingModelResolution(None, None)


def standard_rates_for(billing_model: str | None, input_tokens: int | None) -> PricingRates | None:
    if billing_model is None or input_tokens is None or input_tokens < 0:
        return None
    return STANDARD_API_PRICING.get((billing_model, context_class_for(input_tokens)))


def _component_cost(tokens: int, rate_microusd_per_million: int) -> int:
    amount = (
        Decimal(tokens)
        * Decimal(rate_microusd_per_million)
        / Decimal(TOKENS_PER_MILLION)
    )
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_api_equivalent_cost(
    *,
    usage: UsageBreakdown,
    rates: PricingRates | None,
    builtin_tool_cost_microusd: int | None = 0,
    storage_cost_microusd: int | None = 0,
    container_cost_microusd: int | None = 0,
    legacy_partial: bool = False,
) -> CostEstimate:
    required = (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_write_tokens,
        usage.output_tokens,
    )
    if any(value is None for value in required):
        return CostEstimate(
            EstimateStatus.USAGE_MISSING,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            builtin_tool_cost_microusd,
            storage_cost_microusd,
            container_cost_microusd,
            None,
            None,
            ("Token usage is incomplete.",),
        )

    input_tokens = int(usage.input_tokens)
    cached_tokens = int(usage.cached_input_tokens)
    cache_write_tokens = int(usage.cache_write_tokens)
    output_tokens = int(usage.output_tokens)
    if min(input_tokens, cached_tokens, cache_write_tokens, output_tokens) < 0:
        return _inconsistent_estimate("Token counts cannot be negative.")
    if cached_tokens + cache_write_tokens > input_tokens:
        return _inconsistent_estimate("Cache token counts exceed total input tokens.")
    if rates is None:
        return CostEstimate(
            EstimateStatus.MODEL_UNPRICED,
            context_class_for(input_tokens),
            input_tokens - cached_tokens - cache_write_tokens,
            None,
            None,
            None,
            None,
            None,
            builtin_tool_cost_microusd,
            storage_cost_microusd,
            container_cost_microusd,
            None,
            None,
            ("No Standard API pricing rule matched the billing model.",),
        )

    regular_tokens = input_tokens - cached_tokens - cache_write_tokens
    regular_cost = _component_cost(regular_tokens, rates.input_microusd_per_million)
    cached_cost = _component_cost(cached_tokens, rates.cached_input_microusd_per_million)
    cache_write_cost = _component_cost(cache_write_tokens, rates.cache_write_microusd_per_million)
    output_cost = _component_cost(output_tokens, rates.output_microusd_per_million)
    model_cost = regular_cost + cached_cost + cache_write_cost + output_cost
    charge_components = (builtin_tool_cost_microusd, storage_cost_microusd, container_cost_microusd)
    known_charges = sum(value or 0 for value in charge_components)
    known_total = model_cost + known_charges
    warnings: list[str] = []
    if any(value is None for value in charge_components):
        warnings.append("At least one paid tool, storage, or container charge is unavailable.")
        status = EstimateStatus.TOOL_PRICING_INCOMPLETE
        complete_total = None
    elif legacy_partial:
        warnings.append("Historical audit data does not contain every v2 billing signal.")
        status = EstimateStatus.LEGACY_PARTIAL
        complete_total = None
    else:
        status = EstimateStatus.ESTIMATED_COMPLETE
        complete_total = known_total

    return CostEstimate(
        status=status,
        context_class=context_class_for(input_tokens),
        regular_input_tokens=regular_tokens,
        regular_input_cost_microusd=regular_cost,
        cached_input_cost_microusd=cached_cost,
        cache_write_cost_microusd=cache_write_cost,
        output_cost_microusd=output_cost,
        model_token_cost_microusd=model_cost,
        builtin_tool_cost_microusd=builtin_tool_cost_microusd,
        storage_cost_microusd=storage_cost_microusd,
        container_cost_microusd=container_cost_microusd,
        known_api_equivalent_cost_microusd=known_total,
        api_equivalent_cost_microusd=complete_total,
        warnings=tuple(warnings),
    )


def _inconsistent_estimate(message: str) -> CostEstimate:
    return CostEstimate(
        EstimateStatus.USAGE_INCONSISTENT,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        (message,),
    )


def actual_incremental_cost(
    *,
    billing_mode: BillingMode,
    provider_reported_cost_microusd: int | None = None,
    observed_actual_cost_microusd: int | None = None,
) -> ActualCostResult:
    if observed_actual_cost_microusd is not None:
        return ActualCostResult(ActualCostStatus.OBSERVED, observed_actual_cost_microusd)
    if billing_mode == BillingMode.CHATGPT_SUBSCRIPTION_INCLUDED:
        return ActualCostResult(ActualCostStatus.SUBSCRIPTION_INCLUDED, 0)
    if billing_mode in {BillingMode.OPENAI_API, BillingMode.THIRD_PARTY_API}:
        if provider_reported_cost_microusd is not None:
            return ActualCostResult(ActualCostStatus.PROVIDER_REPORTED, provider_reported_cost_microusd)
    return ActualCostResult(ActualCostStatus.UNKNOWN, None)


def sol_equivalent_savings(
    *,
    usage: UsageBreakdown,
    selected_model_cost_microusd: int | None,
) -> tuple[int | None, int | None]:
    if selected_model_cost_microusd is None or usage.input_tokens is None:
        return None, None
    sol_rates = standard_rates_for("gpt-5.6-sol", usage.input_tokens)
    baseline = calculate_api_equivalent_cost(usage=usage, rates=sol_rates)
    if baseline.api_equivalent_cost_microusd is None:
        return None, None
    savings = max(0, baseline.api_equivalent_cost_microusd - selected_model_cost_microusd)
    return baseline.api_equivalent_cost_microusd, savings
