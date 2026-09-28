"""Páginas da placa: leitura do dia, não um painel."""

from __future__ import annotations

import html

import board_store

CSS = """
:root {
  --ink: #16130f;
  --paper: #f4efe6;
  --quiet: #6f675c;
  --line: #ddd4c6;
  --gold: #8d5a12;
  --gold-soft: #c4a46a;
}
* { box-sizing: border-box; }
html { background: var(--paper); }
body {
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(900px 420px at 100% -10%, rgba(196,164,106,.28), transparent 60%),
    var(--paper);
  font: 18px/1.45 "Iowan Old Style", Palatino, "Palatino Linotype", Georgia, serif;
}
main { max-width: 980px; margin: 0 auto; padding: 28px 22px 96px; }
a { color: inherit; text-decoration: none; }
nav {
  display: flex; gap: 22px; align-items: baseline;
  height: 44px; margin-bottom: 28px;
  font: 13px/1 "Segoe UI", sans-serif;
  letter-spacing: .04em;
}
nav a { color: var(--quiet); border-bottom: 1px solid transparent; padding-bottom: 4px; }
nav a:hover, nav a.on { color: var(--ink); border-color: var(--gold); }
h1 {
  margin: 0;
  font-weight: 500;
  font-size: clamp(48px, 8vw, 92px);
  letter-spacing: -0.045em;
  line-height: 0.92;
}
.kicker { margin: 0 0 10px; color: var(--gold); font: 13px/1.2 "Segoe UI", sans-serif; letter-spacing: .08em; text-transform: uppercase; }
.measure { max-width: 38rem; }
.quiet { color: var(--quiet); }
.hero { display: grid; grid-template-columns: 1.4fr .8fr; gap: 28px; align-items: end; min-height: 42vh; }
.orb {
  width: min(220px, 46vw); height: min(220px, 46vw); border-radius: 50%;
  margin-left: auto;
  background:
    radial-gradient(circle at 34% 30%, #fff8e8, #e7c27a 42%, #8d5a12 74%, #2a1c10 100%);
  box-shadow: inset -18px -22px 40px rgba(42, 28, 16, .35), 0 20px 50px rgba(141, 90, 18, .18);
  animation: breathe 7s ease-in-out infinite;
}
@keyframes breathe { 50% { transform: scale(1.035); } }
@media (prefers-reduced-motion: reduce) { .orb { animation: none; } }
.word {
  font-size: clamp(56px, 9vw, 108px);
  line-height: .9;
  letter-spacing: -0.05em;
  margin: 0;
}
.split { display: grid; grid-template-columns: 1.3fr .7fr; gap: 48px; margin-top: 56px; }
.rule { border: 0; border-top: 1px solid var(--line); margin: 28px 0; }
.time {
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 18px;
  padding: 16px 0;
  border-top: 1px solid var(--line);
}
.time b { font-weight: 500; color: var(--gold); font-variant-numeric: tabular-nums; }
.time strong { font-weight: 500; font-size: 22px; }
.nums { display: grid; grid-template-columns: 1fr 1fr; gap: 22px 28px; }
.num { font-size: clamp(64px, 10vw, 104px); line-height: .85; letter-spacing: -0.06em; font-variant-numeric: tabular-nums; }
.num small { display: block; margin-top: 8px; font: 13px/1.2 "Segoe UI", sans-serif; letter-spacing: .06em; text-transform: uppercase; color: var(--quiet); }
.company {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 16px;
  align-items: baseline;
  padding: 18px 0;
  border-top: 1px solid var(--line);
}
.company em { font-style: normal; font-size: clamp(36px, 5vw, 56px); letter-spacing: -0.04em; }
.index { padding: 18px 0; border-top: 1px solid var(--line); }
.index h2 { margin: 0; font-size: 28px; font-weight: 500; }
.act { font-size: clamp(28px, 4vw, 40px); line-height: 1.15; letter-spacing: -0.03em; max-width: 18ch; }
.note { color: var(--quiet); font-size: 15px; max-width: 42rem; }
@media (max-width: 760px) {
  .hero, .split { grid-template-columns: 1fr; }
  .orb { margin: 12px 0 0; }
  nav { overflow-x: auto; }
}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _shell(title: str, here: str, body: str) -> str:
    links = [
        ("/", "Placa", "placa"),
        ("/agentes", "Agentes", "agentes"),
        ("/board", "Board", "board"),
        ("/board/agenda", "Agenda", "agenda"),
        ("/board/saude", "Saúde", "saude"),
    ]
    nav = []
    for href, label, key in links:
        cls = " class='on'" if key == here else ""
        nav.append(f"<a href='{href}'{cls}>{label}</a>")
    return f"""<!DOCTYPE html>
<html lang="pt-BR"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_e(title)}</title>
<style>{CSS}</style>
</head><body><main>
<nav>{''.join(nav)}</nav>
{body}
</main></body></html>"""


def _ocupacao(value: str) -> str:
    return {"cheio": "Cheio", "equilibrado": "No meio", "livre": "Livre"}.get(value or "", "Aberto")


def agentes() -> str:
    data = board_store.load()
    board = next((item for item in data["agentes"] if item.get("principal") or str(item.get("nome", "")).lower() == "board"), None)
    others = [item for item in data["agentes"] if item is not board]
    hero_name = _e((board or {}).get("nome") or "Board")
    hero_role = _e((board or {}).get("papel") or "Principal da placa.")
    rows = []
    for item in others:
        rows.append(
            f"<div class='index'><h2>{_e(item.get('nome'))}</h2>"
            f"<p class='quiet'>{_e(item.get('papel') or 'Agente do Grok')}</p></div>"
        )
    rest = "".join(rows) if rows else "<p class='quiet'>Os outros agentes aparecem aqui quando o Board os enviar.</p>"
    body = f"""
<p class="kicker">Grok</p>
<section class="hero">
  <div>
    <h1><a href="/board">{hero_name}</a></h1>
    <p class="measure">{hero_role}</p>
  </div>
  <div class="orb" aria-hidden="true"></div>
</section>
<hr class="rule"/>
{rest}
"""
    return _shell("Agentes", "agentes", body)


def board() -> str:
    data = board_store.load()
    agenda = data.get("agenda") or {}
    saude = data.get("saude") or {}
    trabalho = saude.get("trabalho")
    trabalho_txt = "-" if trabalho is None else str(int(trabalho))
    empresas = []
    for item in data["empresas"]:
        nota = "-" if item.get("nota") is None else str(int(item["nota"]))
        empresas.append(
            "<a class='company' href='/board/empresa?nome="
            + html.escape(item["nome"], quote=True)
            + f"'><span>{_e(item['nome'])}</span><em>{_e(nota)}</em></a>"
        )
    empresa_html = "".join(empresas) if empresas else "<p class='quiet'>Cada empresa ganha uma nota quando o Board enviar.</p>"
    body = f"""
<p class="kicker">Principal da placa</p>
<section class="hero">
  <div>
    <h1>Board</h1>
    <p class="measure">A leitura de hoje: o dia, o peso do trabalho, e como você está em cada empresa.</p>
  </div>
  <p class="word">{_e(_ocupacao(agenda.get('ocupacao')))}</p>
</section>
<section class="split">
  <div>
    <p class="kicker">Agenda</p>
    <p class="act"><a href="/board/agenda">{_e(agenda.get('como_agir') or 'O dia ainda não foi lido.')}</a></p>
  </div>
  <div>
    <p class="kicker">Trabalho</p>
    <a href="/board/saude"><div class="num">{_e(trabalho_txt)}<small>de 10</small></div></a>
  </div>
</section>
<hr class="rule"/>
<p class="kicker">Empresas</p>
{empresa_html}
"""
    return _shell("Board", "board", body)


def agenda() -> str:
    data = board_store.load()
    agenda = data.get("agenda") or {}
    rows = []
    for item in agenda.get("compromissos") or []:
        meta = " ".join(part for part in (item.get("onde"), item.get("empresa")) if part)
        meta_html = f"<div class='quiet'>{_e(meta)}</div>" if meta else ""
        rows.append(
            "<div class='time'><b>"
            + _e(item.get("hora") or "-")
            + "</b><div><strong>"
            + _e(item.get("titulo"))
            + "</strong>"
            + meta_html
            + "</div></div>"
        )
    lista = "".join(rows) if rows else "<p class='quiet'>Nenhum compromisso neste dia.</p>"
    when = agenda.get("data") or "Hoje"
    body = f"""
<p class="kicker">{_e(when)}</p>
<h1>{_e(_ocupacao(agenda.get('ocupacao')))}</h1>
<p class="measure">{_e(agenda.get('analise') or 'O Board ainda não disse se o dia está cheio ou livre.')}</p>
<hr class="rule"/>
{lista}
<hr class="rule"/>
<p class="kicker">Como agir</p>
<p class="act">{_e(agenda.get('como_agir') or 'Sem orientação ainda.')}</p>
"""
    return _shell("Agenda", "agenda", body)


def saude() -> str:
    data = board_store.load()
    saude = data.get("saude") or {}

    def figure(label: str, value) -> str:
        shown = "-" if value is None else str(int(value))
        return f"<div><div class='num'>{_e(shown)}<small>{_e(label)}</small></div></div>"

    posterga = "Você está postergando." if saude.get("postergando") else "Você não está postergando."
    if saude.get("trabalho") is None and not saude.get("leitura"):
        posterga = "O Board ainda não leu tarefas, projetos e agenda."
    depressao = saude.get("depressao")
    aviso = ""
    if isinstance(depressao, int) and depressao >= 7:
        aviso = "<hr class='rule'/><p class='act'>Se o peso continuar, fale com alguém. CVV 188.</p>"
    tarefas = "".join(f"<div class='index'>{_e(item)}</div>" for item in saude.get("tarefas") or [])
    projetos = "".join(f"<div class='index'>{_e(item)}</div>" for item in saude.get("projetos") or [])
    body = f"""
<p class="kicker">Leitura, não diagnóstico</p>
<h1>O peso do dia.</h1>
<section class="nums" style="margin-top:36px">
  {figure("Trabalho", saude.get("trabalho"))}
  {figure("Cansaço", saude.get("cansaco"))}
  {figure("Estresse", saude.get("estresse"))}
  {figure("Esgotamento", depressao)}
</section>
<hr class="rule"/>
<p class="act">{_e(posterga)}</p>
<p class="measure">{_e(saude.get('leitura') or '')}</p>
{aviso}
<p class="kicker" style="margin-top:42px">Tarefas</p>
{tarefas or "<p class='quiet'>Nenhuma tarefa nesta leitura.</p>"}
<p class="kicker" style="margin-top:28px">Projetos</p>
{projetos or "<p class='quiet'>Nenhum projeto nesta leitura.</p>"}
<p class="note">O número de esgotamento é o que o Board infere sobre um caminho de depressão, a partir de adiamentos, agenda e carga. Não é um laudo.</p>
"""
    return _shell("Saúde mental", "saude", body)


def plate_text() -> str:
    import unicodedata

    def fold(value) -> str:
        text = unicodedata.normalize("NFKD", "" if value is None else str(value))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.replace("\n", " ").replace("\r", " ")
        return "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text).strip()

    def num(value) -> str:
        return "-" if value is None else str(int(value))

    data = board_store.load()
    agenda = data.get("agenda") or {}
    saude = data.get("saude") or {}
    notes = {}
    for item in data.get("empresas") or []:
        notes[str(item.get("nome", "")).lower()] = num(item.get("nota"))
    lines = [
        "ocupacao=" + fold(_ocupacao(agenda.get("ocupacao"))),
        "agir=" + fold(agenda.get("como_agir") or "Sem leitura"),
        "trabalho=" + num(saude.get("trabalho")),
        "cansaco=" + num(saude.get("cansaco")),
        "estresse=" + num(saude.get("estresse")),
        "esgota=" + num(saude.get("depressao")),
        "vale=" + notes.get("vale", "-"),
        "hatkg=" + notes.get("hatkg", "-"),
        "cpqd=" + notes.get("cpqd", "-"),
        "allied=" + notes.get("alliedit", "-"),
        "ch8=" + notes.get("ch8ai", "-"),
    ]
    return "\n".join(lines) + "\n"


def empresa_page(nome: str) -> str:
    item = board_store.empresa(nome)
    if item is None:
        rows = []
        for empresa in board_store.load()["empresas"]:
            nota = "-" if empresa.get("nota") is None else str(int(empresa["nota"]))
            rows.append(
                "<a class='company' href='/board/empresa?nome="
                + html.escape(empresa["nome"], quote=True)
                + f"'><span>{_e(empresa['nome'])}</span><em>{_e(nota)}</em></a>"
            )
        body = "<p class='kicker'>Empresas</p><h1>Onde você atua.</h1>" + (
            "".join(rows) if rows else "<p class='quiet'>O Board ainda não enviou nenhuma empresa.</p>"
        )
        return _shell("Empresas", "board", body)
    nota = "-" if item.get("nota") is None else str(int(item["nota"]))
    body = f"""
<p class="kicker">Atuação</p>
<h1>{_e(item['nome'])}</h1>
<p class="word" style="margin-top:18px">{_e(nota)}</p>
<p class="quiet">de 0 a 10</p>
<hr class="rule"/>
<p class="act">{_e(item.get('leitura') or 'Sem leitura ainda.')}</p>
"""
    return _shell(item["nome"], "board", body)
