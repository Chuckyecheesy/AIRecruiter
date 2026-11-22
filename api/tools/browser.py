"""Browser research (spec 02). READ. Stretch goal: disabled unless BROWSER_MODE=real."""
from __future__ import annotations

from api.tools.base import ToolError
from api.tools.models import BrowserFindings


class DisabledBrowserTool:
    enabled = False

    async def research(self, urls: list[str], questions: list[str]) -> BrowserFindings:
        raise ToolError("fatal", "browser tool is disabled (BROWSER_MODE=off)")
