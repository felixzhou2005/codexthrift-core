from __future__ import annotations

from codexthrift.billing import (
    ActualCostStatus,
    BillingMode,
    ContextClass,
    EstimateStatus,
    UsageBreakdown,
    actual_incremental_cost,
    calculate_api_equivalent_cost,
    context_class_for,
    resolve_billing_model,
    sol_equivalent_savings,
    standard_rates_for,
)


def test_cache_aware_standard_equivalent_matches_reference_call() -> None:
    usage = UsageBreakdown(120_477, 3_584, 0, 81)

    result = calculate_api_equivalent_cost(
        usage=usage,
        rates=standard_rates_for("gpt-5.6-sol", usage.input_tokens),
    )

    assert result.status == EstimateStatus.ESTIMATED_COMPLETE
    assert result.context_class == ContextClass.SHORT
    assert result.regular_input_tokens == 116_893
    assert result.api_equivalent_cost_microusd == 588_687


def test_long_context_boundary_and_rates_apply_to_entire_request() -> None:
    assert context_class_for(272_000) == ContextClass.SHORT
    assert context_class_for(272_001) == ContextClass.LONG

    short = UsageBreakdown(272_000, 0, 0, 1_000)
    long = UsageBreakdown(272_001, 0, 0, 1_000)
    short_cost = calculate_api_equivalent_cost(
        usage=short,
        rates=standard_rates_for("gpt-5.6-sol", short.input_tokens),
    )
    long_cost = calculate_api_equivalent_cost(
        usage=long,
        rates=standard_rates_for("gpt-5.6-sol", long.input_tokens),
    )

    assert short_cost.api_equivalent_cost_microusd == 1_390_000
    assert long_cost.api_equivalent_cost_microusd == 2_765_010


def test_cache_write_and_reasoning_breakdown_are_not_double_counted() -> None:
    usage = UsageBreakdown(100_000, 10_000, 20_000, 5_000, reasoning_tokens=4_000)

    result = calculate_api_equivalent_cost(
        usage=usage,
        rates=standard_rates_for("gpt-5.6-terra", usage.input_tokens),
    )

    assert result.regular_input_tokens == 70_000
    assert result.regular_input_cost_microusd == 140_000
    assert result.cached_input_cost_microusd == 2_000
    assert result.cache_write_cost_microusd == 50_000
    assert result.output_cost_microusd == 60_000
    assert result.api_equivalent_cost_microusd == 252_000


def test_missing_or_inconsistent_usage_never_becomes_zero_cost() -> None:
    missing = calculate_api_equivalent_cost(
        usage=UsageBreakdown(None, None, None, None),
        rates=None,
    )
    inconsistent = calculate_api_equivalent_cost(
        usage=UsageBreakdown(10, 8, 3, 1),
        rates=standard_rates_for("gpt-5.6-luna", 10),
    )

    assert missing.status == EstimateStatus.USAGE_MISSING
    assert missing.api_equivalent_cost_microusd is None
    assert inconsistent.status == EstimateStatus.USAGE_INCONSISTENT
    assert inconsistent.api_equivalent_cost_microusd is None


def test_model_resolution_uses_observed_model_priority() -> None:
    result = resolve_billing_model(
        response_model="chatgpt/gpt-5.6-terra",
        provider_model="chatgpt/gpt-5.6-sol",
        selected_model="codex-auto",
        selected_alias="luna-low",
    )

    assert result.billing_model == "gpt-5.6-terra"
    assert result.source == "response_model"


def test_actual_incremental_cost_requires_explicit_billing_mode() -> None:
    included = actual_incremental_cost(
        billing_mode=BillingMode.CHATGPT_SUBSCRIPTION_INCLUDED,
        provider_reported_cost_microusd=99,
    )
    unknown = actual_incremental_cost(
        billing_mode=BillingMode.UNKNOWN,
        provider_reported_cost_microusd=0,
    )

    assert included.status == ActualCostStatus.SUBSCRIPTION_INCLUDED
    assert included.amount_microusd == 0
    assert unknown.status == ActualCostStatus.UNKNOWN
    assert unknown.amount_microusd is None


def test_sol_equivalent_savings_uses_same_usage_breakdown() -> None:
    usage = UsageBreakdown(100_000, 20_000, 0, 10_000)
    terra = calculate_api_equivalent_cost(
        usage=usage,
        rates=standard_rates_for("gpt-5.6-terra", usage.input_tokens),
    )

    baseline, savings = sol_equivalent_savings(
        usage=usage,
        selected_model_cost_microusd=terra.model_token_cost_microusd,
    )

    assert baseline == 710_000
    assert savings == 426_000
