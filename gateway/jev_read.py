"""Leituras das páginas via Jev (TypeSafe). Jev devolve escolha, nota e sim/não. O texto sai daqui, em código."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.typesafe.ai/v1/systemone"
SECRET = Path(__file__).resolve().parent / "jev.secret"

SCALE = [
    "Quase nada disso aparece nos fatos.",
    "Aparece pouco, num detalhe isolado.",
    "Aparece em alguns pontos, sem dominar o dia.",
    "Aparece com clareza em mais de um ponto.",
    "Domina boa parte do que foi descrito.",
    "E o traco principal de tudo que foi descrito.",
]

ACOES = {
    "livre": "O dia esta livre. Escolha uma coisa importante e termine antes de abrir outra.",
    "equilibrado": "O dia esta no meio. Proteja os horarios marcados e deixe uma folga entre eles.",
    "cheio": "O dia esta cheio. Nao aceite nada novo. Cumpra o que ja esta marcado e corte o resto.",
}


def _key() -> str:
    if not SECRET.is_file():
        raise RuntimeError("falta gateway/jev.secret")
    return SECRET.read_text(encoding="utf-8").strip()


def _to_ten(score: float) -> int:
    top = len(SCALE) - 1
    if top <= 0:
        return 0
    return max(0, min(10, round(float(score) / top * 10)))


def _state(data: dict) -> str:
    lines = []
    agenda = data.get("agenda") or {}
    if agenda.get("data"):
        lines.append(f"Data: {agenda['data']}")
    for item in agenda.get("compromissos") or []:
        bits = [item.get("hora") or "", item.get("titulo") or "", item.get("empresa") or "", item.get("onde") or ""]
        lines.append("Compromisso: " + " ".join(bit for bit in bits if bit))
    saude = data.get("saude") or {}
    for item in saude.get("tarefas") or []:
        lines.append("Tarefa: " + item)
    for item in saude.get("projetos") or []:
        lines.append("Projeto: " + item)
    for item in data.get("empresas") or []:
        if item.get("contexto"):
            lines.append(f"Empresa {item.get('nome')}: {item['contexto']}")
    return "\n".join(lines)[:12000]


def _questions(companies: list[str]) -> dict:
    questions = {
        "ocupacao": {
            "type": "choice",
            "instructions": "Como esta a ocupacao deste dia, olhando so os compromissos?",
            "criteria": {
                "livre": "Poucos ou nenhum compromisso, sobra tempo.",
                "equilibrado": "Ha compromissos, com folga entre eles.",
                "cheio": "Os horarios estao tomados ou se atropelam.",
            },
        },
        "trabalho": {
            "type": "score",
            "instructions": "Qual o nivel de carga de trabalho nestes fatos?",
            "criteria": SCALE,
        },
        "cansaco": {
            "type": "score",
            "instructions": "Quanto cansaco estes fatos mostram?",
            "criteria": SCALE,
        },
        "estresse": {
            "type": "score",
            "instructions": "Quanto estresse estes fatos mostram?",
            "criteria": SCALE,
        },
        "esgotamento": {
            "type": "score",
            "instructions": "Quanto estes fatos mostram um caminho de esgotamento, com adiamento, isolamento e carga que nao fecha? Nao e um diagnostico.",
            "criteria": SCALE,
        },
        "postergando": {
            "type": "noul",
            "instructions": "Estes fatos mostram que a pessoa esta postergando o que importa.",
        },
    }
    for index, name in enumerate(companies[:8]):
        questions[f"empresa_{index}"] = {
            "type": "score",
            "instructions": f"Como esta a atuacao da pessoa na empresa {name}, segundo estes fatos?",
            "criteria": SCALE,
        }
    return questions


def ask(state: str, companies: list[str]) -> dict:
    body = json.dumps(
        {"state": state, "model": "jev-latest", "questions": _questions(companies)},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        headers={"Authorization": "Bearer " + _key(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Jev recusou a leitura ({exc.code})") from exc
    return payload.get("answers") or {}


def fill(data: dict) -> str | None:
    state = _state(data)
    if len(state.strip()) < 8:
        return None
    companies = [item.get("nome") or "" for item in data.get("empresas") or [] if item.get("nome")]
    answers = ask(state, companies)
    ocupacao = ((answers.get("ocupacao") or {}).get("choice") or "").strip().lower()
    if ocupacao not in ACOES:
        ocupacao = "equilibrado"
    agenda = data.setdefault("agenda", {})
    agenda["ocupacao"] = ocupacao
    agenda["como_agir"] = ACOES[ocupacao]
    agenda["analise"] = {
        "livre": "O dia esta livre. Sobra espaco entre o que esta marcado.",
        "equilibrado": "O dia esta no meio. Ha compromisso e tambem folga.",
        "cheio": "O dia esta cheio. Os horarios se encostam.",
    }[ocupacao]
    saude = data.setdefault("saude", {})
    for key in ("trabalho", "cansaco", "estresse"):
        raw = (answers.get(key) or {}).get("score")
        saude[key] = _to_ten(raw) if raw is not None else None
    raw_burn = (answers.get("esgotamento") or {}).get("score")
    saude["depressao"] = _to_ten(raw_burn) if raw_burn is not None else None
    noul = (answers.get("postergando") or {}).get("noul")
    saude["postergando"] = bool(noul is not None and float(noul) >= 0.5)
    saude["leitura"] = (
        f"Trabalho {saude['trabalho']} de 10. Cansaco {saude['cansaco']} de 10. "
        f"Estresse {saude['estresse']} de 10. Esgotamento {saude['depressao']} de 10. "
        + ("Ha postergacao." if saude["postergando"] else "Nao ha postergacao clara.")
    )
    for index, item in enumerate((data.get("empresas") or [])[:8]):
        raw = (answers.get(f"empresa_{index}") or {}).get("score")
        if raw is None:
            continue
        item["nota"] = _to_ten(raw)
        item["leitura"] = f"Atuacao em {item['nome']}: {item['nota']} de 10, lida pelo Jev a partir dos fatos."
    return None
