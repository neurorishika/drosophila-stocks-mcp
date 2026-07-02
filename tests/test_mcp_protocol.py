"""End-to-end MCP protocol tests: launch the real server process and speak
MCP over both transports, exactly as a real client would.

Offline-safe: uses ``FLYBASE_STOCKS_FILE`` pointing at the bundled fixture, so
no network is required and results are deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import closing

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_stocks.tsv")

EXPECTED_TOOLS = {
    "search_stocks_by_genotype",
    "search_stocks_by_gene",
    "get_stock",
    "list_stock_centers",
    "resolve_gene",
    "get_dataset_info",
}


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stdio_env():
    env = dict(os.environ)
    env["FLYBASE_STOCKS_FILE"] = FIXTURE
    env.pop("MCP_TRANSPORT", None)
    return env


async def _run_stdio_session(env):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "drosophila_stocks_mcp"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == EXPECTED_TOOLS
            for t in tools.tools:
                assert t.description and t.description.strip()
                assert t.inputSchema and t.inputSchema.get("type") == "object"

            centers = await session.call_tool("list_stock_centers", {})
            centers_payload = json.loads(centers.content[0].text)
            assert len(centers_payload["centers"]) >= 7
            assert any(c["code"] == "BDSC" for c in centers_payload["centers"])

            stock = await session.call_tool("get_stock", {"identifier": "FBst0041157"})
            stock_payload = json.loads(stock.content[0].text)
            assert stock_payload["found"] is True
            assert stock_payload["center_code"] == "BDSC"
            assert "mir-932" in stock_payload["genotype"]

            search = await session.call_tool(
                "search_stocks_by_genotype", {"query": "mir-932"}
            )
            search_payload = json.loads(search.content[0].text)
            assert search_payload["count"] >= 1
            assert any(
                "mir-932" in r["genotype"] for r in search_payload["results"]
            )
            return True


def test_stdio_transport_lists_and_calls_tools(stdio_env):
    assert asyncio.run(_run_stdio_session(stdio_env))


def test_stdio_transport_stdout_is_clean_jsonrpc(stdio_env):
    """stdio is JSON-RPC-over-stdout; any stray text (print/log) would corrupt it.

    Launch the server as a raw subprocess, send a valid initialize request, and
    assert every line the process writes to stdout parses as JSON.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "drosophila_stocks_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=stdio_env,
        text=True,
        bufsize=1,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.0"},
            },
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        deadline = time.time() + 15
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.strip():
                break
        assert line.strip(), "no response on stdout before timeout"
        parsed = json.loads(line)
        assert parsed.get("jsonrpc") == "2.0"
        assert parsed.get("id") == 1
        assert "result" in parsed
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _run_http_session(url: str):
    async with streamable_http_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == EXPECTED_TOOLS
            return True


def test_streamable_http_transport_lists_tools(stdio_env):
    port = _free_port()
    env = dict(stdio_env)
    env["MCP_TRANSPORT"] = "streamable-http"
    env["MCP_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "drosophila_stocks_mcp"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        deadline = time.time() + 15
        last_exc = None
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=1)
                break
            except Exception as exc:  # noqa: BLE001 - polling until server is up
                last_exc = exc
                time.sleep(0.3)
        else:
            proc.kill()
            out, err = proc.communicate(timeout=5)
            raise RuntimeError(f"server never came up: {last_exc}\nstdout={out}\nstderr={err}")

        assert asyncio.run(_run_http_session(url))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
