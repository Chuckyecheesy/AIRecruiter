"""Shared LLM helper for structured Anthropic and Gemini calls."""
from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

import anthropic
from google import genai
from google.genai import errors as genai_errors
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

    def __init__(
        self,
        api_key: str | None,
        model: str,
        provider: Literal["anthropic", "gemini"] | None = None,
    ):
        from api.config import settings

        self.model = model
        self.provider = provider or settings.llm_provider
        if self.provider == "gemini":
            key = settings.gemini_api_key if provider is None else api_key
            self._client = genai.Client(api_key=key) if key else genai.Client()
        else:
            self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    async def structured(
        self, *, system: str, prompt: str, output: type[T], max_tokens: int = 4000, effort: str = "low"
    ) -> T:
        if self.provider == "gemini":
            return await self._structured_gemini(
                system=system, prompt=prompt, output=output, max_tokens=max_tokens
            )
        return await self._structured_anthropic(
            system=system, prompt=prompt, output=output, max_tokens=max_tokens, effort=effort
        )

    async def _structured_anthropic(
        self, *, system: str, prompt: str, output: type[T], max_tokens: int, effort: str
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

    async def _structured_gemini(
        self, *, system: str, prompt: str, output: type[T], max_tokens: int
    ) -> T:
        try:
            resp = await self._client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    response_mime_type="application/json",
                    response_schema=output,
                ),
            )
        except genai_errors.ClientError as e:
            message = str(e)
            if e.code == 429:
                raise ToolError("rate_limited", "gemini rate limited", retry_after_s=5.0) from e
            if e.code in (400, 422):
                raise ToolError("invalid_input", f"gemini: {message}") from e
            raise ToolError("fatal", f"gemini: {message}") from e
        except genai_errors.ServerError as e:
            raise ToolError("transient", f"gemini server error: {e}") from e
        except genai_errors.APIError as e:
            raise ToolError("transient", f"gemini API error: {e}") from e

        if resp.parsed is not None:
            return output.model_validate(resp.parsed)
        if resp.text:
            return output.model_validate_json(resp.text)
        raise ToolError("transient", "gemini output incomplete")
