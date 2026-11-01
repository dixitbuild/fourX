"""A small synchronous MCP client: start the tool server, list its tools, call them."""
import asyncio
import json
import os
import sys
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpClient:
    def __init__(self) -> None:
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error:
            raise RuntimeError(f"Could not start the tool server: {self._error}")

    _error: BaseException | None = None

    async def _serve(self) -> None:
        """Keep one server process and one session alive until close() is called."""
        self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        params = StdioServerParameters(command=sys.executable, args=["-m", "fourx.mcp_server"], env=dict(os.environ))
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    def _run(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def tools(self) -> list[dict[str, Any]]:
        """The server's tools as {name, description, parameters}, ready to hand to a model."""
        listed = self._run(self._session.list_tools()).tools
        return [{"name": tool.name, "description": tool.description or "", "parameters": tool.inputSchema} for tool in listed]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool. Always returns a dict with a "status" key; a failed call is an issue."""
        try:
            result = self._run(self._session.call_tool(name, arguments))
        except Exception as exc:
            return {"status": "issue", "message": f"The {name} tool failed: {exc}"}
        if result.structuredContent:
            return result.structuredContent
        text = "".join(getattr(part, "text", "") for part in result.content)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "status" in parsed:
                return parsed
        except ValueError:
            pass
        return {"status": "issue", "message": text or f"The {name} tool returned nothing."}

    def close(self) -> None:
        if self._error is None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)
