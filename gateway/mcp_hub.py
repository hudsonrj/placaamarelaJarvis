"""Clientes MCP locais (stdio e HTTP) usados pelo assistente."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "mcp.json"
PROTOCOL = "2024-11-05"


def _slug(server: str, tool: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9_]+", "_", f"{server}_{tool}").strip("_")
    if not raw:
        raw = "tool"
    if raw[0].isdigit():
        raw = "t_" + raw
    return raw[:64]


def _read_config() -> list[dict]:
    if not CONFIG.is_file():
        return []
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = data.get("servers") if isinstance(data, dict) else None
    return [item for item in servers or [] if isinstance(item, dict)]


def _write_config(servers: list[dict]) -> None:
    CONFIG.write_text(json.dumps({"servers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")


def _flatten(result) -> str:
    if isinstance(result, str):
        return result[:4000]
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)[:4000]
    content = result.get("content")
    if isinstance(content, list):
        bits = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                bits.append(str(item.get("text") or ""))
            elif isinstance(item, dict):
                bits.append(json.dumps(item, ensure_ascii=False))
            else:
                bits.append(str(item))
        text = "\n".join(bits).strip()
        if text:
            return text[:4000]
    if result.get("isError"):
        return "a ferramenta retornou erro"
    return json.dumps(result, ensure_ascii=False)[:4000]


class StdioClient:
    def __init__(self, spec: dict):
        self.spec = spec
        self.proc: subprocess.Popen | None = None
        self.pending: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()
        self.next_id = 1
        self.last_error = ""
        self.alive = False

    def start(self) -> list[dict]:
        command = (self.spec.get("command") or "").strip()
        if not command:
            raise RuntimeError("comando vazio")
        args = [str(item) for item in (self.spec.get("args") or [])]
        exe = shutil.which(command) or command
        env = os.environ.copy()
        extra = self.spec.get("env") or {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                env[str(key)] = str(value)
        shell = exe.lower().endswith((".cmd", ".bat"))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        argv = subprocess.list2cmdline([exe, *args]) if shell else [exe, *args]
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=shell,
            bufsize=0,
            creationflags=flags,
        )
        self.alive = True
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "mysoul-placa", "version": "1.0"},
            },
            timeout=40,
        )
        self.notify("notifications/initialized")
        tools: list[dict] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            listed = self.request("tools/list", params, timeout=40) or {}
            tools.extend(listed.get("tools") or [])
            cursor = listed.get("nextCursor")
            if not cursor:
                break
        return tools

    def close(self) -> None:
        self.alive = False
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def request(self, method: str, params: dict | None, timeout: float = 30) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(self.last_error or "processo MCP encerrou")
        number = self.next_id
        self.next_id += 1
        box: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[number] = box
        self._send({"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}})
        try:
            message = box.get(timeout=timeout)
        except queue.Empty as exc:
            with self.lock:
                self.pending.pop(number, None)
            raise TimeoutError(method) from exc
        if isinstance(message, dict) and message.get("error"):
            raise RuntimeError(str(message["error"])[:400])
        return (message or {}).get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def call(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=45)
        return _flatten(result)

    def _send(self, message: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("sem stdin")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self.lock:
            proc.stdin.write(header + body)
            proc.stdin.flush()

    def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.last_error = text[-400:]

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        buffer = b""
        while self.alive and proc.poll() is None:
            try:
                message, buffer = _pull_message(proc.stdout, buffer)
            except Exception as exc:
                self.last_error = str(exc)[:400]
                break
            if message is None:
                break
            number = message.get("id")
            if number is None:
                continue
            with self.lock:
                box = self.pending.pop(number, None)
            if box is not None:
                box.put(message)
        self.alive = False


class HttpClient:
    def __init__(self, spec: dict):
        self.url = (spec.get("url") or "").strip()
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }
        self.next_id = 1

    def start(self) -> list[dict]:
        if not self.url:
            raise RuntimeError("URL vazia")
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "mysoul-placa", "version": "1.0"},
            },
            timeout=30,
        )
        self.notify("notifications/initialized")
        listed = self.request("tools/list", {}, timeout=30) or {}
        return listed.get("tools") or []

    def close(self) -> None:
        return None

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._post(message, timeout=15)

    def request(self, method: str, params: dict | None, timeout: float = 30) -> dict:
        number = self.next_id
        self.next_id += 1
        message = self._post(
            {"jsonrpc": "2.0", "id": number, "method": method, "params": params or {}},
            timeout=timeout,
        )
        if message.get("error"):
            raise RuntimeError(str(message["error"])[:400])
        return message.get("result") or {}

    def call(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=45)
        return _flatten(result)

    def _post(self, message: dict, timeout: float) -> dict:
        raw = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(self.url, data=raw, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.headers["Mcp-Session-Id"] = session
                body = response.read().decode("utf-8", "replace")
                ctype = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(detail or str(exc)) from exc
        if "text/event-stream" in ctype:
            return _sse_json(body)
        if not body.strip():
            return {}
        return json.loads(body)


def _sse_json(body: str) -> dict:
    found = None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            found = json.loads(payload)
        except json.JSONDecodeError:
            continue
    return found or {}


def _pull_message(stream, buffer: bytes) -> tuple[dict | None, bytes]:
    while True:
        if not buffer:
            chunk = stream.read(1)
            if not chunk:
                return None, buffer
            buffer += chunk
        while buffer[:1] in (b"\r", b"\n", b" "):
            buffer = buffer[1:]
        if buffer.startswith(b"Content-Length") or buffer.startswith(b"content-length"):
            while b"\r\n\r\n" not in buffer and b"\n\n" not in buffer:
                chunk = stream.read(1)
                if not chunk:
                    return None, buffer
                buffer += chunk
                if len(buffer) > 65536:
                    raise RuntimeError("cabecalho MCP grande demais")
            if b"\r\n\r\n" in buffer:
                header, buffer = buffer.split(b"\r\n\r\n", 1)
            else:
                header, buffer = buffer.split(b"\n\n", 1)
            match = re.search(br"(?i)content-length:\s*(\d+)", header)
            if not match:
                continue
            need = int(match.group(1))
            while len(buffer) < need:
                chunk = stream.read(need - len(buffer))
                if not chunk:
                    return None, buffer
                buffer += chunk
            body, buffer = buffer[:need], buffer[need:]
            return json.loads(body.decode("utf-8")), buffer
        if buffer.startswith(b"{"):
            depth = 0
            in_string = False
            escape = False
            index = 0
            while True:
                while index >= len(buffer):
                    chunk = stream.read(512)
                    if not chunk:
                        return None, buffer
                    buffer += chunk
                char = buffer[index : index + 1]
                index += 1
                if in_string:
                    if escape:
                        escape = False
                    elif char == b"\\":
                        escape = True
                    elif char == b'"':
                        in_string = False
                    continue
                if char == b'"':
                    in_string = True
                elif char == b"{":
                    depth += 1
                elif char == b"}":
                    depth -= 1
                    if depth == 0:
                        body, buffer = buffer[:index], buffer[index:]
                        return json.loads(body.decode("utf-8")), buffer
            continue
        buffer = buffer[1:]


class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients: dict[str, object] = {}
        self.runtime: dict[str, dict] = {}
        self.routes: dict[str, tuple[str, str]] = {}

    def servers(self) -> list[dict]:
        return _read_config()

    def snapshot(self) -> dict:
        with self.lock:
            runtime = []
            for spec in _read_config():
                info = self.runtime.get(spec.get("id") or "", {})
                runtime.append(
                    {
                        "id": spec.get("id"),
                        "ok": bool(info.get("ok")),
                        "error": info.get("error") or "",
                        "tools": info.get("tools") or [],
                    }
                )
            return {"servers": self.servers(), "runtime": runtime}

    def save(self, servers: list[dict]) -> dict:
        clean = []
        for item in servers:
            if not isinstance(item, dict):
                continue
            transport = item.get("transport") if item.get("transport") in ("stdio", "http") else "stdio"
            env = item.get("env") if isinstance(item.get("env"), dict) else {}
            args = item.get("args") if isinstance(item.get("args"), list) else []
            clean.append(
                {
                    "id": str(item.get("id") or uuid.uuid4()),
                    "name": str(item.get("name") or "MCP").strip()[:80] or "MCP",
                    "transport": transport,
                    "command": str(item.get("command") or "").strip(),
                    "args": [str(arg) for arg in args][:40],
                    "url": str(item.get("url") or "").strip(),
                    "env": {str(key): str(value) for key, value in env.items()},
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        _write_config(clean)
        self.refresh_async()
        return self.snapshot()

    def upsert(self, spec: dict) -> dict:
        servers = self.servers()
        spec = dict(spec)
        if not spec.get("id"):
            spec["id"] = str(uuid.uuid4())
        replaced = False
        for index, item in enumerate(servers):
            if item.get("id") == spec["id"]:
                servers[index] = {**item, **spec, "id": item["id"]}
                replaced = True
                break
        if not replaced:
            servers.append(spec)
        return self.save(servers)

    def delete(self, server_id: str) -> dict:
        return self.save([item for item in self.servers() if item.get("id") != server_id])

    def set_enabled(self, server_id: str, enabled: bool) -> dict:
        servers = self.servers()
        for item in servers:
            if item.get("id") == server_id:
                item["enabled"] = bool(enabled)
        return self.save(servers)

    def refresh_async(self) -> None:
        threading.Thread(target=self.refresh, daemon=True).start()

    def refresh(self) -> None:
        wanted = {item["id"]: item for item in self.servers() if item.get("enabled")}
        with self.lock:
            current = list(self.clients.keys())
        for server_id in current:
            if server_id not in wanted:
                self._stop(server_id)
        for server_id, spec in wanted.items():
            with self.lock:
                running = server_id in self.clients and self.runtime.get(server_id, {}).get("ok")
            if not running:
                self._start(spec)
        self._rebuild_routes()

    def restart_one(self, server_id: str) -> dict:
        spec = next((item for item in self.servers() if item.get("id") == server_id), None)
        self._stop(server_id)
        if spec and spec.get("enabled", True):
            self._start(spec)
        elif spec:
            with self.lock:
                self.runtime[server_id] = {"ok": False, "error": "desligado", "tools": []}
        self._rebuild_routes()
        return self.snapshot()

    def openai_tools(self) -> list[dict]:
        with self.lock:
            items = []
            for info in self.runtime.values():
                if not info.get("ok"):
                    continue
                for tool in info.get("raw") or []:
                    qname = tool.get("_qname")
                    if not qname:
                        continue
                    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object", "properties": {}}
                    items.append(
                        {
                            "type": "function",
                            "function": {
                                "name": qname,
                                "description": str(tool.get("description") or tool.get("name") or "")[:300],
                                "parameters": schema,
                            },
                        }
                    )
            return items[:12]

    def call(self, qname: str, arguments: dict) -> str:
        with self.lock:
            route = self.routes.get(qname)
            client = self.clients.get(route[0]) if route else None
        if route is None or client is None:
            raise RuntimeError("ferramenta indisponivel")
        return client.call(route[1], arguments or {})

    def close_all(self) -> None:
        with self.lock:
            ids = list(self.clients.keys())
        for server_id in ids:
            self._stop(server_id)

    def _start(self, spec: dict) -> None:
        server_id = spec["id"]
        self._stop(server_id)
        client = HttpClient(spec) if spec.get("transport") == "http" else StdioClient(spec)
        try:
            tools = client.start()
        except Exception as exc:
            client.close()
            with self.lock:
                self.runtime[server_id] = {"ok": False, "error": str(exc)[:400], "tools": [], "raw": []}
            print(f"mcp {spec.get('name')}: {exc}")
            return
        named = []
        public = []
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            qname = _slug(spec.get("name") or "mcp", str(tool["name"]))
            copy = dict(tool)
            copy["_qname"] = qname
            named.append(copy)
            public.append({"name": qname, "title": str(tool.get("name"))})
        with self.lock:
            self.clients[server_id] = client
            self.runtime[server_id] = {"ok": True, "error": "", "tools": public, "raw": named}
        print(f"mcp {spec.get('name')}: {len(public)} ferramentas")

    def _stop(self, server_id: str) -> None:
        with self.lock:
            client = self.clients.pop(server_id, None)
            self.runtime.pop(server_id, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _rebuild_routes(self) -> None:
        routes = {}
        with self.lock:
            for info in self.runtime.values():
                for tool in info.get("raw") or []:
                    qname = tool.get("_qname")
                    # route filled below from clients keys is awkward; store server on tool
            for server_id, info in self.runtime.items():
                for tool in info.get("raw") or []:
                    qname = tool.get("_qname")
                    if qname:
                        routes[qname] = (server_id, str(tool.get("name")))
            self.routes = routes
