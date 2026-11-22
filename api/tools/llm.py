"""Shared Claude helper for lane B's LLM tools (drafter, evaluator, rubric, persona, classifier)."""
from __future__ import annotations

import json
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from api.tools.base import ToolError

T = TypeVar("T", bound=BaseModel)


def to_jsonable(obj: Any) -> Any:
    """Accept Pydantic models, dicts, lists, or plain objects (lane A's Job/CandidateMemory)."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), indent=2, default=str)


class LLM:
    """Thin async wrapper: one structured-output call, errors mapped to ToolError."""

    def __init__(self, api_key: str | None, model: str):
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    async def structured(
        self, *, system: str, prompt: str, output: type[T], max_tokens: int = 4000, effort: str = "low"
    ) -> T:
        try:
            resp = await self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=output,
                output_config={"effort": effort},
            )
        except anthropic.RateLimitError as e:
            ra = e.response.headers.get("retry-after")
            raise ToolError("rate_limited", "claude rate limited", retry_after_s=float(ra) if ra else 5.0) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise ToolError("fatal", f"claude: {e.message}") from e
        except anthropic.BadRequestError as e:
            # Billing / credit problems come back as 400s but re-deciding won't fix them.
            kind = "fatal" if "credit balance" in str(e.message).lower() else "invalid_input"
            raise ToolError(kind, f"claude: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise ToolError("transient", f"claude {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ToolError("transient", "claude connection error") from e

        if resp.stop_reason == "refusal":
            raise ToolError("fatal", "claude refused the request")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise ToolError("transient", f"claude output incomplete (stop_reason={resp.stop_reason})")
        return resp.parsed_output
