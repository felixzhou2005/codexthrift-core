from __future__ import annotations

import os


ROUTING_CALLBACK_SOURCE = r'''from __future__ import annotations

import os
import time
from collections import OrderedDict
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

import codex_routing_policy as policy


TTL_SECONDS = 7200
MAX_DECISIONS = 1000


def _phase3a_alias(tier: str, selected_alias: str) -> str:
    if selected_alias == "sol-xhigh":
        return selected_alias
    return {
        "SIMPLE": "luna-low",
        "MEDIUM": "terra-medium",
        "COMPLEX": "sol-medium",
        "REASONING": "sol-high",
    }.get(tier, selected_alias)


def _effort_for_alias(alias: str) -> str:
    return {
        "luna-low": "low",
        "sol-high": "high",
        "sol-xhigh": "xhigh",
    }.get(alias, "medium")


class Phase21RouteController(CustomLogger):
    def __init__(self) -> None:
        self._decisions: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._response_to_turn: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._audit_routes: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._audit_routes_by_fingerprint: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._audit_routes_by_call_id: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def _prune(self) -> None:
        cutoff = time.monotonic() - TTL_SECONDS
        for cache in (
            self._decisions,
            self._response_to_turn,
            self._audit_routes,
            self._audit_routes_by_fingerprint,
            self._audit_routes_by_call_id,
        ):
            expired = [key for key, value in cache.items() if value[0] < cutoff]
            for key in expired:
                cache.pop(key, None)
            while len(cache) > MAX_DECISIONS:
                cache.popitem(last=False)

    def _remember(self, decision: dict[str, Any]) -> None:
        turn_id = decision.get("logical_turn_id")
        if not turn_id:
            return
        self._decisions[str(turn_id)] = (time.monotonic(), decision)
        self._decisions.move_to_end(str(turn_id))
        self._prune()

    def _remember_audit_route(self, route: dict[str, Any], data: dict[str, Any]) -> None:
        turn_id = route.get("logical_turn_id")
        if turn_id:
            self._audit_routes[str(turn_id)] = (time.monotonic(), dict(route))
            self._audit_routes.move_to_end(str(turn_id))
        try:
            fingerprint = policy.analyze_request(data).prompt_fingerprint
        except Exception:
            fingerprint = None
        if fingerprint:
            self._audit_routes_by_fingerprint[str(fingerprint)] = (
                time.monotonic(),
                dict(route),
            )
            self._audit_routes_by_fingerprint.move_to_end(str(fingerprint))
        call_id = data.get("litellm_call_id")
        if call_id:
            self._audit_routes_by_call_id[str(call_id)] = (
                time.monotonic(),
                dict(route),
            )
            self._audit_routes_by_call_id.move_to_end(str(call_id))
        self._prune()

    def audit_route_for(
        self,
        data: dict[str, Any],
        call_id: Any = None,
    ) -> dict[str, Any] | None:
        if call_id:
            self._prune()
            value = self._audit_routes_by_call_id.get(str(call_id))
            if value is not None:
                return dict(value[1])
        try:
            analyzed = policy.analyze_request(data)
        except Exception:
            return None
        turn_id = analyzed.logical_turn_id
        if not turn_id:
            fingerprint = analyzed.prompt_fingerprint
            if fingerprint:
                self._prune()
                value = self._audit_routes_by_fingerprint.get(str(fingerprint))
                if value is not None:
                    return dict(value[1])
            return None
        self._prune()
        value = self._audit_routes.get(turn_id)
        if value is not None:
            return dict(value[1])
        fingerprint = analyzed.prompt_fingerprint
        if fingerprint:
            value = self._audit_routes_by_fingerprint.get(str(fingerprint))
            if value is not None:
                return dict(value[1])
        cached = self._cached_for(turn_id, data.get("previous_response_id"))
        return dict(cached) if cached is not None else None

    def _cached_for(self, turn_id: str | None, previous_response_id: Any) -> dict[str, Any] | None:
        self._prune()
        if turn_id and turn_id in self._decisions:
            _, decision = self._decisions[turn_id]
            self._decisions[turn_id] = (time.monotonic(), decision)
            self._decisions.move_to_end(turn_id)
            return dict(decision)
        if previous_response_id is not None:
            response_key = str(previous_response_id)
            cached_lineage = self._response_to_turn.get(response_key)
            if cached_lineage is not None:
                _, lineage_turn = cached_lineage
                return self._cached_for(lineage_turn, None)
        return None

    @staticmethod
    def _metadata(data: dict[str, Any]) -> dict[str, Any]:
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        return metadata

    @staticmethod
    def _attach_to_logging_object(data: dict[str, Any], route: dict[str, Any]) -> None:
        logging_obj = data.get("litellm_logging_obj")
        details = getattr(logging_obj, "model_call_details", None)
        if not isinstance(details, dict):
            return
        for owner in (details, details.setdefault("litellm_params", {})):
            if not isinstance(owner, dict):
                continue
            metadata = owner.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                owner["metadata"] = metadata
            metadata["codex_router"] = dict(route)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict) or data.get("model") != "codex-auto":
            return data

        analyzed = policy.analyze_request(data).to_dict()
        control = policy.strip_control_prefix_from_request(data)
        turn_id = analyzed.get("logical_turn_id")
        cached = self._cached_for(turn_id, data.get("previous_response_id"))
        has_user_text = bool(policy.extract_last_user_text(data))
        if cached is not None:
            decision = cached
            request_kind = "tool_continuation"
            routing_cause = "turn_inheritance"
        elif has_user_text:
            decision = analyzed
            request_kind = "user_turn"
            routing_cause = str(decision.get("routing_cause") or "phase21_heuristic")
            self._remember(decision)
        else:
            decision = analyzed
            decision.update({
                "tier": "MEDIUM",
                "selected_alias": "terra-medium",
                "routing_mode": "auto",
                "routing_cause": "safe_default",
                "score": 0.5,
                "signals": ["no_user_no_lineage"],
                "override": None,
            })
            request_kind = "unknown"
            routing_cause = "safe_default"

        if control and control not in {"auto", "自动"} and cached is not None:
            # A manual prefix must always win, including clients that reuse a turn id.
            decision = analyzed
            request_kind = "user_turn"
            routing_cause = "manual_override"
            self._remember(decision)

        selected_alias = str(decision["selected_alias"])
        if os.environ.get("CODEX_ROUTER_STATIC_PHASE") == "phase3a":
            selected_alias = _phase3a_alias(str(decision.get("tier") or "MEDIUM"), selected_alias)
        data["model"] = selected_alias
        data["reasoning_effort"] = _effort_for_alias(selected_alias)
        metadata = self._metadata(data)
        route_metadata = {
            **decision,
            "audit_schema_version": 2,
            "request_kind": request_kind,
            "routing_cause": routing_cause,
            "requested_alias": "codex-auto",
            "selected_alias": selected_alias,
            "reasoning_effort": data["reasoning_effort"],
            "default_reason": "no_user_no_lineage" if routing_cause == "safe_default" else None,
        }
        metadata["codex_router"] = route_metadata
        self._attach_to_logging_object(data, route_metadata)
        self._remember_audit_route(route_metadata, data)
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            params = kwargs.get("litellm_params") or {}
            metadata = (
                params.get("metadata")
                or params.get("litellm_metadata")
                or kwargs.get("metadata")
                or kwargs.get("litellm_metadata")
                or {}
            )
            route = metadata.get("codex_router") if isinstance(metadata, dict) else None
            if not isinstance(route, dict):
                return
            self._remember(route)
            response_id = getattr(response_obj, "id", None)
            turn_id = route.get("logical_turn_id")
            if response_id and turn_id:
                self._response_to_turn[str(response_id)] = (time.monotonic(), str(turn_id))
                self._response_to_turn.move_to_end(str(response_id))
                self._prune()
        except Exception:
            return


route_controller = Phase21RouteController()
'''
