import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.core.config import settings


class McpClientError(RuntimeError):
    pass


class StdioMcpClient:
    def __init__(self, command: str | None = None, args: list[str] | None = None, cwd: str | None = None) -> None:
        backend_root = Path(__file__).resolve().parents[2]
        self.command = command or sys.executable
        self.args = args or [str(backend_root / "app" / "mcp" / "memory_server.py")]
        self.cwd = cwd or str(backend_root)
        self._next_id = 1

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        process = await self._start()
        try:
            await self._initialize(process)
            await self._notify(process, "notifications/initialized", {})
            return await self._request(process, "tools/call", {"name": name, "arguments": arguments})
        finally:
            await self._stop(process)

    async def inspect_server(self) -> dict[str, Any]:
        """Perform a real MCP handshake and return the server's advertised tools."""
        process = await self._start()
        try:
            initialize = await self._initialize(process)
            await self._notify(process, "notifications/initialized", {})
            tools = await self._request(process, "tools/list", {})
            return {"initialize": initialize, "tools": tools.get("tools") or []}
        finally:
            await self._stop(process)

    async def _initialize(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        return await self._request(
            process,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "devflow-web-agent", "version": "0.1.0"},
            },
        )

    async def _start(self) -> asyncio.subprocess.Process:
        # 子进程只拿必需变量：不继承服务端的全部 API key。
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "PYTHONIOENCODING": "utf-8",
            "DATABASE_URL": settings.database_url,
            "AUTO_SYNC_ENABLED": "false",
        }
        process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # 后台排空 stderr：服务端写满管道缓冲会导致 readline 永久阻塞。
        self._drain_stderr(process)
        return process

    def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return

        async def _consume() -> None:
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        return
            except Exception:
                return

        try:
            asyncio.get_running_loop().create_task(_consume())
        except RuntimeError:
            pass

    async def _notify(self, process: asyncio.subprocess.Process, method: str, params: dict[str, Any]) -> None:
        await self._write(process, {"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, process: asyncio.subprocess.Process, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._write(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if process.stdout is None:
            raise McpClientError("MCP process has no stdout")
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
        except TimeoutError as exc:
            raise McpClientError(f"MCP request timed out: {method}") from exc
        if not line:
            # stderr 由后台任务持续排空，这里不再读取（避免与排空任务竞争）。
            raise McpClientError("MCP server closed stdout")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpClientError(f"MCP server returned an invalid JSON response for {method}") from exc
        if "error" in response:
            error = response["error"]
            raise McpClientError(str(error.get("message") or error))
        return response.get("result") or {}

    async def _write(self, process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise McpClientError("MCP process has no stdin")
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
                return
        else:
            await process.wait()
