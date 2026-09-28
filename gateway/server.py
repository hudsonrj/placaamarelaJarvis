"""Gateway da placa: orb, MySoul, Atomic Chat, áudio e página de MCP."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audio
import board_pages
import board_store
import mcp_hub
import mysoul

HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("MYSOUL_PORT", "8791"))
UDP_PORT = int(os.environ.get("MYSOUL_DISCOVERY", "8792"))
SERIAL_PORT = os.environ.get("MYSOUL_COM", "COM5")
ATOMIC = "http://127.0.0.1:1337/v1"
MODEL = "chaoliangUNSW/Jev-Style-Qwen3_5-2B-Decision-Q4_K_M"
ROOT = Path(__file__).resolve().parent
WEBHOOK_FILE = ROOT / "webhook.json"
AGENT_NAME = "orquestrador"
PAGE = (ROOT / "page.html").read_text(encoding="utf-8")

hub = mcp_hub.Hub()
state_lock = threading.Lock()
cable_lock = threading.Lock()
cable_out: list[bytes] = []
state = {
    "state": "idle",
    "line": "Toque para ouvir",
    "screen": "",
    "heard": "",
    "answer": "",
    "busy": False,
    "inputs_used": [],
    "outputs_used": [],
    "error": "",
}
atomic_cache = {"t": 0.0, "ok": False}


def usable_phrase(text: str) -> bool:
    words = re.findall(r"[A-Za-zÀ-ÿ0-9']+", text or "")
    if len(words) < 2 or len((text or "").strip()) < 8:
        return False
    lowered = f" {(text or '').lower()} "
    if re.search(r"[áàâãéêíóôõúç]", lowered):
        return True
    hints = (
        " que ", " nao ", " não ", " para ", " com ", " uma ", " voce ", " você ",
        " como ", " esta ", " está ", " meu ", " dia ", " isso ", " agora ",
        " saude ", " saúde ", " empresa ",
    )
    if any(hint in lowered for hint in hints):
        return True
    english = {"the", "is", "are", "was", "were", "right", "fighter", "ultimate", "he", "and", "of", "to", "a"}
    tokens = [word.lower() for word in words]
    if sum(word in english for word in tokens) >= max(1, len(tokens) // 2):
        return False
    return len(words) >= 3


def clean_reply(text: str) -> str:
    raw = text or ""
    if "Resposta:" in raw:
        raw = raw.split("Resposta:")[-1]
    raw = raw.split("---")[0]
    raw = re.split(r"\n\s*\[", raw)[0]
    raw = re.sub(r"Fatos da placa:.*", " ", raw, flags=re.S)
    raw = re.sub(r"Trechos do MySoul:.*", " ", raw, flags=re.S)
    raw = re.sub(r"\[[^\]]{0,140}\]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -:;\n")
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", raw) if part.strip()]
    kept: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(sentence)
        if len(kept) == 2:
            break
    raw = " ".join(kept).strip()
    if not usable_phrase(raw):
        return ""
    return raw[:320]


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("\n", " ").replace("\r", " ")
    return "".join(ch if 32 <= ord(ch) < 127 else " " for ch in value)


def set_state(**fields) -> None:
    with state_lock:
        state.update(fields)


def snapshot() -> dict:
    with state_lock:
        return dict(state)


_last_push = ""


def push_cable(text: str) -> None:
    global _last_push
    payload = text if text.endswith("\n") else text + "\n"
    with cable_lock:
        if payload == _last_push:
            return
        _last_push = payload
        cable_out.append(payload.encode("utf-8"))


def drain_cable() -> list[bytes]:
    with cable_lock:
        pending = list(cable_out)
        cable_out.clear()
    return pending


def atomic_up() -> bool:
    import time

    now = time.time()
    if now - atomic_cache["t"] < 5:
        return atomic_cache["ok"]
    ok = False
    try:
        with urllib.request.urlopen(ATOMIC + "/models", timeout=1.5) as response:
            ok = response.status == 200
    except (OSError, urllib.error.URLError):
        ok = False
    atomic_cache.update(t=now, ok=ok)
    return ok


def visible(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, dict):
                bits.append(str(part.get("text") or ""))
            else:
                bits.append(str(part))
        content = "".join(bits)
    content = str(content)
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[-1]
    elif "<think>" in content:
        content = ""
    content = re.sub(r"</?think>", "", content).strip()
    return content


def ask_model(question: str, context: str, role: str = "") -> str:
    facts = board_store.facts_text()
    system = (
        "Voce e a voz da MySoul na placa amarela. Fale portugues do Brasil, "
        "em duas frases curtas, proprias para serem lidas em voz alta. "
        "Responda somente em portugues, com o que os trechos ensinam. "
        "Nao copie trechos, nomes de arquivo, ingles nem a pergunta. "
        "Se a pergunta for sobre o dia, a saude ou uma empresa, use os fatos da placa. "
        "Nao invente citacao. Nao de diagnostico medico nem oriente dose de remedio."
    )
    if role:
        system += " " + role
    user = (
        f"Fatos da placa:\n{facts}\n\n"
        f"Trechos do MySoul:\n{context or '(nenhum trecho encontrado)'}\n\n"
        f"Pergunta: {question}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    tools = hub.openai_tools()
    for _ in range(3):
        payload = {
            "model": MODEL,
            "messages": messages,
            "max_tokens": 280,
            "temperature": 0.3,
            "repeat_penalty": 1.2,
            "frequency_penalty": 0.4,
            "stop": ["\nTrechos", "\nFatos", "\n---", "\n["],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            message = chat(payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if tools and exc.code in (400, 422):
                print("modelo recusou ferramentas, segue sem elas:", detail)
                tools = []
                continue
            raise RuntimeError(detail or str(exc)) from exc
        calls = message.get("tool_calls") or []
        if not calls:
            text = visible(message)
            if text:
                return text
            nudged = chat(
                {
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": "Responda em portugues, em uma frase, sem bloco de raciocinio."},
                        {"role": "user", "content": question},
                    ],
                    "max_tokens": 160,
                    "temperature": 0.3,
                    "repeat_penalty": 1.2,
                    "stop": ["\n[", "\n---"],
                }
            )
            return visible(nudged) or "Nao consegui formular a resposta."
        messages.append(message)
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            try:
                result = hub.call(name, args if isinstance(args, dict) else {})
            except Exception as exc:
                result = f"erro na ferramenta: {exc}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": result[:4000],
                }
            )
    return "Nao consegui fechar a resposta."


def chat(payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ATOMIC + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer atomic"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]


def turn() -> None:
    try:
        set_state(state="listening", line="Ouvindo", screen="", error="")
        heard, inputs = audio.listen(8)
        set_state(heard=heard, inputs_used=inputs)
        if not heard:
            set_state(state="idle", line="Toque o orb", screen="Nao ouvi", error="")
            return
        print("ouviu:", heard)
        set_state(state="thinking", line="Pensando", screen=fold(heard)[:160])
        context = mysoul.retrieve(heard)
        answer = ask_model(heard, context).strip()
        print("respondeu:", answer[:180])
        screen = fold(answer)[:160]
        set_state(state="speaking", line="Falando", screen=screen, answer=answer)
        outputs = audio.speak(answer)
        set_state(outputs_used=outputs, state="idle", line="Toque o orb")
    except Exception as exc:
        traceback.print_exc()
        set_state(state="idle", line="Toque o orb", screen="Falhou", error=str(exc)[:240], answer="")
    finally:
        with state_lock:
            state["busy"] = False
            if state["state"] != "idle":
                state["state"] = "idle"
                state["line"] = "Toque o orb"


hold_stop = None
speak_stop = threading.Event()


def held_turn(stop) -> None:
    try:
        samples, rate = audio.record_until(stop, 30)
        set_state(state="thinking", line="Pensando", screen="")
        push_cable("line=Pensando\n")
        heard = audio.transcribe(samples, rate)
        set_state(heard=heard)
        if not usable_phrase(heard):
            set_state(state="idle", line="Toque para ouvir", screen="Nao ouvi")
            push_cable("line=Toque para ouvir\nscreen=Nao ouvi\n")
            return
        print("ouviu:", heard)
        context = mysoul.retrieve(heard)
        answer = clean_reply(ask_model(heard, context))
        print("respondeu:", answer[:180])
        if speak_stop.is_set():
            set_state(state="idle", line="Toque para ouvir", screen="")
            push_cable("line=Toque para ouvir\nscreen=\n")
            return
        if not answer:
            set_state(state="idle", line="Toque para ouvir", screen="Nao entendi")
            push_cable("line=Toque para ouvir\nscreen=Nao entendi. Fale de novo.\n")
            return
        shown = fold(answer)[:78]
        set_state(state="speaking", line="Falando", screen=shown, answer=answer)
        push_cable(f"line=Falando\nscreen={shown}\n")
        outputs = audio.speak(answer)
        if speak_stop.is_set():
            set_state(state="idle", line="Toque para ouvir", screen="")
            push_cable("line=Toque para ouvir\nscreen=\n")
            return
        set_state(outputs_used=outputs, state="idle", line="Toque para ouvir")
        push_cable(f"line=Toque para ouvir\nscreen={shown}\n")
    except Exception as exc:
        traceback.print_exc()
        set_state(state="idle", line="Toque para ouvir", screen="Falhou", error=str(exc)[:240])
        push_cable("line=Toque para ouvir\nscreen=Falhou\n")
    finally:
        with state_lock:
            state["busy"] = False
            if state["state"] != "idle":
                state["state"] = "idle"
                state["line"] = "Toque para ouvir"


def orb_press() -> None:
    global hold_stop
    with state_lock:
        if state["state"] == "listening" and hold_stop is not None:
            hold_stop.set()
            state["state"] = "thinking"
            state["line"] = "Pensando"
            return
        if state["busy"]:
            speak_stop.set()
            if hold_stop is not None:
                hold_stop.set()
            state["state"] = "idle"
            state["line"] = "Toque para ouvir"
            state["screen"] = ""
            push_outside = True
        else:
            push_outside = False
            speak_stop.clear()
            state["busy"] = True
            state["state"] = "listening"
            state["line"] = "Ouvindo"
            state["screen"] = ""
            hold_stop = threading.Event()
            stop = hold_stop
    if push_outside:
        push_cable("line=Toque para ouvir\nscreen=\n")
        return
    threading.Thread(target=held_turn, args=(stop,), daemon=True).start()


def begin_turn() -> bool:
    with state_lock:
        if state["busy"]:
            return False
        state["busy"] = True
    threading.Thread(target=turn, daemon=True).start()
    return True


def speak_test() -> bool:
    with state_lock:
        if state["busy"]:
            return False
        state["busy"] = True
        state["state"] = "speaking"
        state["line"] = "Falando"
    def run() -> None:
        try:
            outputs = audio.speak(
                "MySoul esta ouvindo pelo computador, pelo bluetooth e pelos dispositivos ligados."
            )
            set_state(outputs_used=outputs)
        finally:
            set_state(state="idle", line="Toque o orb", busy=False)
    threading.Thread(target=run, daemon=True).start()
    return True


def load_webhook() -> dict:
    if WEBHOOK_FILE.is_file():
        try:
            data = json.loads(WEBHOOK_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    else:
        data = {}
    if not data.get("token"):
        data["token"] = secrets.token_urlsafe(32)
    data["agent"] = AGENT_NAME
    agents = []
    for item in data.get("agents") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        agents.append(
            {
                "name": name[:80],
                "url": url,
                "token": str(item.get("token") or ""),
            }
        )
    data["agents"] = agents
    WEBHOOK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def save_agents(agents: list[dict]) -> dict:
    current = load_webhook()
    current["agents"] = agents
    current["agent"] = AGENT_NAME
    WEBHOOK_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def local_urls() -> list[str]:
    found = [f"http://127.0.0.1:{HTTP_PORT}/hooks/grokbot"]
    ips: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ips.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    seen = set()
    for ip in ips:
        if not ip or ip.startswith("127.") or ip.startswith("169.254.") or ip.startswith("172.") or ip in seen:
            continue
        seen.add(ip)
        found.append(f"http://{ip}:{HTTP_PORT}/hooks/grokbot")
    return found


def webhook_public() -> dict:
    data = load_webhook()
    urls = local_urls()
    return {
        "agent": AGENT_NAME,
        "url": urls[0],
        "urls": urls,
        "token": data["token"],
        "authorization": "Bearer " + data["token"],
        "agents": data["agents"],
    }


def authorized(handler: BaseHTTPRequestHandler) -> bool:
    expected = load_webhook()["token"]
    header = handler.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        given = header[7:].strip()
    else:
        given = (handler.headers.get("X-Webhook-Token") or "").strip()
    if not given:
        query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
        for part in query.split("&"):
            if part.startswith("token="):
                given = urllib.request.unquote(part[6:])
                break
    return secrets.compare_digest(given, expected)


def message_of(data: dict) -> str:
    for key in ("message", "text", "prompt", "input", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = data.get("data")
    if isinstance(nested, dict):
        return message_of(nested)
    return ""


def forward_agent(name: str, message: str) -> str:
    data = load_webhook()
    spec = next((item for item in data["agents"] if item["name"].lower() == name.lower()), None)
    if spec is None:
        known = ", ".join(item["name"] for item in data["agents"]) or "nenhum"
        return f"Nao achei o agente {name}. Ligados: {known}."
    body = json.dumps(
        {"message": message, "from": AGENT_NAME, "agent": AGENT_NAME},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if spec.get("token"):
        headers["Authorization"] = "Bearer " + spec["token"]
    request = urllib.request.Request(spec["url"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as exc:
        return f"{name} nao respondeu: {exc}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:2000] or f"{name} respondeu vazio."
    if isinstance(payload, dict):
        for key in ("reply", "message", "text", "output"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key].strip()
    return raw.strip()[:2000]


def wants_display(data: dict) -> bool:
    if data.get("raw") is True or str(data.get("raw")).strip().lower() in ("1", "true", "sim"):
        return True
    kind = str(data.get("type") or data.get("event") or "").strip().lower()
    return kind in ("display", "aviso", "show", "raw")


def show_notice(data: dict) -> dict:
    text = ""
    for key in ("text", "message", "notice", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    if not text:
        text = message_of(data)
    if not text:
        return {"ok": False, "agent": AGENT_NAME, "error": "texto vazio"}
    shown = fold(text)[:150]
    current = snapshot()
    if current.get("screen") != shown or current.get("answer") != text:
        set_state(heard=text, answer=text, screen=shown, line="Toque para ouvir", state="idle")
        push_cable(f"reply={shown}\nline=Toque para ouvir\nscreen={shown}\n")
    speak = data.get("speak") is True or str(data.get("speak")).strip().lower() in ("1", "true", "sim")
    if speak:
        threading.Thread(target=_speak_board, args=(text,), daemon=True).start()
    return {
        "ok": True,
        "agent": AGENT_NAME,
        "reply": text,
        "message": text,
        "text": text,
        "displayed": True,
    }


def orchestrate(data: dict) -> dict:
    kind = str(data.get("type") or data.get("event") or "").lower()
    if kind == "ping":
        return {"ok": True, "agent": AGENT_NAME, "reply": "pong", "message": "pong", "text": "pong"}
    if wants_display(data):
        return show_notice(data)
    if kind in ("feed", "pagina", "paginas") or any(key in data for key in ("agenda", "saude", "saude_mental", "empresas")):
        result = board_store.apply(data)
        reply = result.get("reply") or "Paginas atualizadas."
        if data.get("speak"):
            set_state(state="speaking", line="Falando", screen=fold(reply)[:160], answer=reply)
            try:
                audio.speak(reply)
            finally:
                set_state(state="idle", line="Toque o orb")
        else:
            set_state(answer=reply, screen=fold(reply)[:160])
        return result
    message = message_of(data)
    if not message:
        return {"ok": False, "agent": AGENT_NAME, "error": "mensagem vazia"}
    target = str(data.get("to") or "").strip()
    if data.get("broadcast"):
        replies = []
        for agent in load_webhook()["agents"]:
            replies.append(f"{agent['name']}: {forward_agent(agent['name'], message)}")
        reply = "\n".join(replies) if replies else "Nenhum outro agente ligado."
    elif target and target.lower() not in (AGENT_NAME, "mysoul"):
        reply = forward_agent(target, message)
    else:
        roster = load_webhook()["agents"]
        names = ", ".join(item["name"] for item in roster) or "nenhum ainda"
        context = mysoul.retrieve(message)
        reply = ask_model(
            message,
            context,
            role=(
                f"Voce e o agente {AGENT_NAME} e conversa com os outros agentes: {names}. "
                "Responda curto, para ser repassado."
            ),
        )
    reply = (reply or "").strip()
    if data.get("speak"):
        set_state(state="speaking", line="Falando", screen=fold(reply)[:160], heard=message, answer=reply)
        try:
            audio.speak(reply)
        finally:
            set_state(state="idle", line="Toque o orb")
    else:
        set_state(heard=message, answer=reply, screen=fold(reply)[:160])
    return {"ok": True, "agent": AGENT_NAME, "reply": reply, "message": reply, "text": reply}


_board_recent = {"key": "", "at": 0.0, "shown": ""}
_board_lock = threading.Lock()


def board_answer(message: str) -> str:
    text = fold(message).lower()
    data = board_store.load()
    agenda = data.get("agenda") if isinstance(data.get("agenda"), dict) else {}
    saude = data.get("saude") if isinstance(data.get("saude"), dict) else {}
    empresas = [item for item in (data.get("empresas") or []) if isinstance(item, dict)]
    if "cansad" in text:
        return "Saude cansada. Reduza o ritmo."
    if "saude boa" in text:
        return "Saude boa. Mantenha o ritmo."
    if "saude" in text and "meio" in text:
        return "Saude no meio. Faca pausas."
    if "empresa cheia" in text or "firma cheia" in text:
        return "Firma cheia. Feche uma pendencia."
    if "empresa livre" in text or "firma livre" in text:
        return "Firma livre. Avance um contrato."
    if "empresa" in text and "meio" in text:
        return "Firma no meio. Escolha uma entrega."
    if "dia cheio" in text or text.strip() == "cheio":
        return "Dia cheio. Corte uma tarefa."
    if "dia livre" in text or text.strip() == "livre":
        return "Dia livre. Avance uma coisa."
    if "dia no meio" in text or text.strip() == "meio":
        return "Dia no meio. Faca uma e pare."
    if "saude" in text:
        if all(saude.get(key) is None for key in ("trabalho", "cansaco", "estresse", "depressao")):
            return "Saude ainda sem leitura."
        return "Trabalho {t}, cansaco {c}, estresse {e}.".format(
            t=saude.get("trabalho", "-"),
            c=saude.get("cansaco", "-"),
            e=saude.get("estresse", "-"),
        )
    if "empresa" in text or "firma" in text or "nota" in text:
        notas = [f"{item.get('nome')} {item.get('nota')}" for item in empresas if item.get("nota") is not None]
        if notas:
            return ", ".join(notas)
        return "Empresas sem nota ainda."
    if any(word in text for word in ("dia", "agenda", "agora", "fazer")):
        ocupacao = str(agenda.get("ocupacao") or "").strip()
        agir = str(agenda.get("como_agir") or "").strip()
        if ocupacao or agir:
            return f"{ocupacao}. {agir}".strip(" .")
        return "Dia ainda sem leitura."
    return "Toque Dia, Saude ou Firmas."


def answer_board(message: str, push: bool = True) -> str:
    key = (message or "").strip()
    now = time.time()
    with _board_lock:
        if key and key == _board_recent["key"] and now - _board_recent["at"] < 20:
            return _board_recent["shown"]
        reply = board_answer(key)
        shown = fold(reply)[:42]
        _board_recent.update(key=key, at=now, shown=shown)
    print("board:", key, "->", shown)
    set_state(heard=key, answer=reply, screen=shown, line="Toque para ouvir", state="idle")
    if push:
        push_cable(f"reply={shown}\nopt1=Dia\nopt2=Saude\nopt3=Firmas\nline=Toque para ouvir\nscreen={shown}\n")
    threading.Thread(target=_speak_board, args=(reply,), daemon=True).start()
    return shown


def _speak_board(reply: str) -> None:
    try:
        audio.speak(reply)
    except Exception as exc:
        print("board voz:", exc)


def bard_reply(text: str) -> bytes:
    message = (text or "").strip()[:240]
    reply = answer_board(message)
    options = ("Dia", "Saude", "Firmas")
    record = {"at": time.strftime("%H:%M"), "from": "Board", "message": message, "reply": reply}
    path = ROOT / "bard_log.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError):
        previous = []
    if not isinstance(previous, list):
        previous = []
    previous.append(record)
    path.write_text(json.dumps(previous[-20:], ensure_ascii=False, indent=2), encoding="utf-8")
    threading.Thread(target=forward_bard, args=(message,), daemon=True).start()
    set_state(heard=message, answer=reply, screen=fold(reply)[:160])
    lines = [f"reply={fold(reply)}", f"opt1={fold(options[0])}", f"opt2={fold(options[1])}", f"opt3={fold(options[2])}"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def forward_bard(message: str) -> None:
    agents = [item for item in (load_webhook().get("agents") or []) if str(item.get("url") or "").strip()]
    if not agents:
        print("grok: o agente Board ainda nao tem endereco de webhook")
        return
    for agent in agents:
        url = str(agent.get("url") or "")
        if not url:
            continue
        body = json.dumps({"message": message, "from": "Board", "agent": "Board"}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = str(agent.get("token") or "")
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            urllib.request.urlopen(request, timeout=8).read()
        except (OSError, urllib.error.URLError):
            continue


def health() -> dict:
    now = snapshot()
    return {
        "ok": True,
        "model": MODEL,
        "atomic": atomic_up(),
        "mysoul": mysoul.stats(),
        "state": now["state"],
        "line": now["line"],
        "heard": now["heard"],
        "answer": now["answer"],
        "inputs": now["inputs_used"],
        "outputs": now["outputs_used"],
        "error": now["error"],
    }


def plate_state() -> bytes:
    now = snapshot()
    lines = [
        f"state={now['state']}",
        f"line={fold(now['line'])[:40]}",
        f"screen={fold(now['screen'])[:160]}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/v1/state"):
            return
        print(f"{self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json(200, health())
        elif path == "/api/audio":
            self._json(200, audio.describe())
        elif path == "/api/mcp":
            self._json(200, hub.snapshot())
        elif path == "/api/webhook":
            self._json(200, webhook_public())
        elif path == "/agentes":
            self._send(200, board_pages.agentes(), "text/html; charset=utf-8")
        elif path == "/board":
            self._send(200, board_pages.board(), "text/html; charset=utf-8")
        elif path == "/board/agenda":
            self._send(200, board_pages.agenda(), "text/html; charset=utf-8")
        elif path == "/board/saude":
            self._send(200, board_pages.saude(), "text/html; charset=utf-8")
        elif path == "/board/empresa":
            nome = ""
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("nome="):
                        nome = urllib.request.unquote(part[5:].replace("+", " "))
            self._send(200, board_pages.empresa_page(nome), "text/html; charset=utf-8")
        elif path == "/api/board":
            self._json(200, board_store.load())
        elif path == "/v1/pages":
            self._send(200, board_pages.plate_text(), "text/plain; charset=utf-8")
        elif path == "/v1/state":
            self._send(200, plate_state(), "text/plain; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json(404, {"error": "nao encontrado"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self._json(413, {"error": "corpo grande demais"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        if path == "/v1/bard":
            self._send(200, bard_reply(raw.decode("utf-8", "replace")), "text/plain; charset=utf-8")
            return
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if path == "/v1/tap":
            before = snapshot()["state"]
            orb_press()
            after = snapshot()["state"]
            if before == "listening" or after == "listening":
                label = "Pensando" if before == "listening" else "Toque p/ parar"
                push_cable(f"line={label}\n")
            self._json(200, {"ok": True, "state": after})
        elif path == "/api/speak":
            started = speak_test()
            self._json(200 if started else 409, {"ok": started, "error": "" if started else "ocupado"})
        elif path == "/api/mcp":
            self._mcp(data)
        elif path == "/api/mcp/test":
            server_id = str(data.get("id") or "")
            threading.Thread(target=hub.restart_one, args=(server_id,), daemon=True).start()
            self._json(202, {"ok": True})
        elif path == "/api/webhook":
            self._webhook_config(data)
        elif path == "/hooks/grokbot":
            if not authorized(self):
                self._json(401, {"ok": False, "error": "token invalido"})
                return
            try:
                self._json(200, orchestrate(data))
            except Exception as exc:
                traceback.print_exc()
                self._json(500, {"ok": False, "agent": AGENT_NAME, "error": str(exc)[:300]})
        else:
            self._json(404, {"error": "nao encontrado"})

    def _webhook_config(self, data: dict) -> None:
        op = data.get("op") or "save"
        current = load_webhook()
        agents = list(current["agents"])
        if op == "delete":
            name = str(data.get("name") or "")
            agents = [item for item in agents if item["name"].lower() != name.lower()]
        elif op == "save":
            spec = data.get("agent") if isinstance(data.get("agent"), dict) else data
            name = str(spec.get("name") or "").strip()
            url = str(spec.get("url") or "").strip()
            if not name or not url:
                self._json(400, {"error": "nome e endereco do outro agente"})
                return
            item = {"name": name[:80], "url": url, "token": str(spec.get("token") or "")}
            agents = [old for old in agents if old["name"].lower() != name.lower()]
            agents.append(item)
        save_agents(agents)
        self._json(200, webhook_public())

    def _mcp(self, data: dict) -> None:
        op = data.get("op") or "save"
        try:
            if op == "delete":
                result = hub.delete(str(data.get("id") or ""))
            elif op == "enabled":
                result = hub.set_enabled(str(data.get("id") or ""), bool(data.get("enabled")))
            else:
                server = data.get("server") if isinstance(data.get("server"), dict) else data
                result = hub.upsert(server)
        except Exception as exc:
            self._json(400, {"error": str(exc)[:300]})
            return
        self._json(200, result)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _send(self, code: int, body, content_type: str) -> None:
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def udp_loop() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, UDP_PORT))
    print(f"descoberta UDP {UDP_PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(256)
        except OSError:
            continue
        if data.startswith(b"MYSOUL"):
            sock.sendto(f"MYSOUL {HTTP_PORT}".encode("ascii"), addr)


def serial_pc() -> None:
    try:
        import serial
    except Exception:
        print("porta serial indisponivel")
        return
    while True:
        try:
            link = serial.Serial()
            link.port = SERIAL_PORT
            link.baudrate = 115200
            link.timeout = 0.3
            link.dtr = False
            link.rts = False
            link.open()
            link.dtr = False
            link.rts = False
        except Exception:
            time.sleep(2)
            continue
        buffer = ""
        greeted = False
        print(f"placa no cabo {SERIAL_PORT}")
        try:
            while True:
                for blob in drain_cable():
                    link.write(blob)
                chunk = link.read(200)
                if not chunk:
                    continue
                buffer += chunk.decode("utf-8", "replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    text = line.strip()
                    if text == "TAP":
                        before = snapshot()["state"]
                        orb_press()
                        after = snapshot()["state"]
                        print("orb toque", before, "->", after)
                        if before == "listening":
                            link.write(b"line=Pensando\n")
                        elif after == "listening":
                            link.write(b"line=Toque p/ parar\n")
                        continue
                    if text.startswith("ASK "):
                        shown = answer_board(text[4:].strip(), push=False)
                        link.write(f"reply={shown}\nline=Toque para ouvir\nscreen={shown}\n".encode("utf-8"))
                        continue
                    if "MYSOUL" not in text:
                        continue
                    if not greeted:
                        greeted = True
                        link.write(b"line=Toque para ouvir\n")
        except Exception:
            try:
                link.close()
            except Exception:
                pass
            time.sleep(1)


def main() -> None:
    mysoul.stats()
    hub.refresh_async()
    threading.Thread(target=serial_pc, daemon=True).start()
    threading.Thread(target=udp_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    print(f"MySoul http://127.0.0.1:{HTTP_PORT}")
    print(f"modelo {MODEL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        hub.close_all()
        server.shutdown()


if __name__ == "__main__":
    main()
