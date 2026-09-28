"""Estado que o agente Board envia para as páginas da placa."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parent / "board_state.json"
EMPRESAS = ["Vale", "Hatkg", "CPQD", "AlliedIT", "CH8ai"]

EMPTY = {
    "updated": "",
    "agentes": [
        {
            "nome": "Board",
            "papel": "Principal da placa. Recebe a agenda, a saúde mental e as empresas.",
            "principal": True,
        }
    ],
    "agenda": {},
    "saude": {},
    "empresas": [{"nome": nome, "nota": None, "contexto": "", "leitura": ""} for nome in EMPRESAS],
}


def _blank(nome: str) -> dict:
    return {"nome": nome, "nota": None, "contexto": "", "leitura": ""}


def ensure_companies(items: list) -> list:
    by_name = {}
    extras = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        nome = str(item.get("nome") or "").strip()
        if not nome:
            continue
        key = nome.lower()
        if key in {known.lower() for known in EMPRESAS}:
            by_name[key] = item
        else:
            extras.append(item)
    ordered = []
    for nome in EMPRESAS:
        current = by_name.get(nome.lower()) or _blank(nome)
        current["nome"] = nome
        ordered.append(current)
    ordered.extend(extras)
    return ordered


def _clamp(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(10, number))


def facts_text() -> str:
    data = load()
    agenda = data.get("agenda") if isinstance(data.get("agenda"), dict) else {}
    saude = data.get("saude") if isinstance(data.get("saude"), dict) else {}
    ocupacao = str(agenda.get("ocupacao") or agenda.get("estado") or "").strip()
    agir = str(agenda.get("como_agir") or agenda.get("agir") or "").strip()
    bits = []
    if ocupacao or agir:
        bits.append(f"Agenda: {ocupacao or 'sem leitura'}. {agir}".strip())
    else:
        bits.append("Agenda: ainda sem leitura de hoje.")
    if saude:
        bits.append(
            "Saude: trabalho {trabalho}, cansaco {cansaco}, estresse {estresse}, esgotamento {depressao}.".format(
                trabalho=saude.get("trabalho", "-"),
                cansaco=saude.get("cansaco", "-"),
                estresse=saude.get("estresse", "-"),
                depressao=saude.get("depressao", "-"),
            )
        )
    else:
        bits.append("Saude: ainda sem leitura.")
    empresas = []
    for item in data.get("empresas") or []:
        if not isinstance(item, dict):
            continue
        nome = str(item.get("nome") or "").strip()
        if not nome:
            continue
        nota = item.get("nota")
        empresas.append(f"{nome} {nota}" if nota is not None else nome)
    bits.append("Empresas: " + ", ".join(empresas) + ".")
    return " ".join(bits)


def load() -> dict:
    data = json.loads(json.dumps(EMPTY))
    if PATH.is_file():
        try:
            saved = json.loads(PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        if isinstance(saved, dict):
            data.update({key: saved.get(key, data[key]) for key in EMPTY})
            data["updated"] = saved.get("updated") or ""
    data["empresas"] = ensure_companies(data.get("empresas") or [])
    if not any(str(item.get("nome", "")).lower() == "board" for item in data["agentes"] if isinstance(item, dict)):
        data["agentes"].insert(0, EMPTY["agentes"][0])
    return data


def save(data: dict) -> dict:
    data["updated"] = datetime.now().isoformat(timespec="minutes")
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _items(value) -> list:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def apply(payload: dict) -> dict:
    data = load()
    if isinstance(payload.get("agentes"), list):
        incoming = []
        for item in _items(payload["agentes"]):
            nome = str(item.get("nome") or item.get("name") or "").strip()
            if not nome:
                continue
            incoming.append(
                {
                    "nome": nome[:80],
                    "papel": str(item.get("papel") or item.get("role") or "")[:240],
                    "principal": bool(item.get("principal")) or nome.lower() == "board",
                }
            )
        if incoming and not any(item["principal"] for item in incoming):
            for item in incoming:
                if item["nome"].lower() == "board":
                    item["principal"] = True
        if incoming:
            data["agentes"] = incoming
    agenda = payload.get("agenda")
    if isinstance(agenda, dict):
        compromissos = []
        for item in _items(agenda.get("compromissos") or agenda.get("itens") or agenda.get("eventos")):
            titulo = str(item.get("titulo") or item.get("title") or "").strip()
            if not titulo:
                continue
            compromissos.append(
                {
                    "hora": str(item.get("hora") or item.get("inicio") or "")[:16],
                    "titulo": titulo[:160],
                    "onde": str(item.get("onde") or item.get("local") or "")[:80],
                    "empresa": str(item.get("empresa") or "")[:80],
                }
            )
        ocupacao = str(agenda.get("ocupacao") or agenda.get("cheio") or "").strip().lower()
        if ocupacao not in ("cheio", "equilibrado", "livre"):
            ocupacao = ""
        data["agenda"] = {
            "data": str(agenda.get("data") or "")[:20],
            "compromissos": compromissos[:24],
            "ocupacao": ocupacao,
            "analise": str(agenda.get("analise") or "")[:1200],
            "como_agir": str(agenda.get("como_agir") or agenda.get("acao") or "")[:800],
        }
    saude = payload.get("saude") or payload.get("saude_mental")
    if isinstance(saude, dict):
        postergando = saude.get("postergando")
        if isinstance(postergando, str):
            postergando = postergando.strip().lower() in ("sim", "true", "1", "alto", "muito")
        data["saude"] = {
            "trabalho": _clamp(saude.get("trabalho")),
            "cansaco": _clamp(saude.get("cansaco") or saude.get("fadiga")),
            "estresse": _clamp(saude.get("estresse")),
            "postergando": bool(postergando),
            "depressao": _clamp(saude.get("depressao") or saude.get("esgotamento")),
            "leitura": str(saude.get("leitura") or saude.get("analise") or "")[:1500],
            "tarefas": [str(item)[:160] for item in (saude.get("tarefas") or []) if str(item).strip()][:12],
            "projetos": [str(item)[:160] for item in (saude.get("projetos") or []) if str(item).strip()][:12],
        }
    if isinstance(payload.get("empresas"), list):
        empresas = []
        for item in _items(payload["empresas"]):
            nome = str(item.get("nome") or item.get("name") or "").strip()
            if not nome:
                continue
            empresas.append(
                {
                    "nome": nome[:80],
                    "nota": None,
                    "contexto": str(item.get("contexto") or item.get("leitura") or item.get("analise") or "")[:800],
                    "leitura": "",
                }
            )
        data["empresas"] = ensure_companies(empresas)
    try:
        import jev_read

        jev_read.fill(data)
    except Exception:
        agenda = data.setdefault("agenda", {})
        if not agenda.get("analise"):
            agenda["analise"] = "O Jev nao completou a leitura."
            agenda["como_agir"] = "Tente de novo em alguns minutos."
    saved = save(data)
    return {"ok": True, "agent": "Board", "updated": saved["updated"], "reply": spoken(saved), "message": spoken(saved), "text": spoken(saved)}


def spoken(data: dict) -> str:
    parts = []
    agenda = data.get("agenda") or {}
    if agenda.get("ocupacao"):
        parts.append(f"O dia está {agenda['ocupacao']}.")
    if agenda.get("como_agir"):
        parts.append(str(agenda["como_agir"]))
    saude = data.get("saude") or {}
    if saude.get("trabalho") is not None:
        parts.append(f"Nível de trabalho {saude['trabalho']} de 10.")
    if not parts:
        parts.append("Páginas do Board atualizadas.")
    return " ".join(parts)[:500]


def empresa(nome: str) -> dict | None:
    wanted = (nome or "").strip().lower()
    if not wanted:
        return None
    for item in load()["empresas"]:
        if str(item.get("nome", "")).lower() == wanted:
            return item
    return None
