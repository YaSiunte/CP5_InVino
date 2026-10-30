"""
Dashboard CP5 - Edge Computing: monitoramento de vinherias (tema preto e dourado)

O que ele faz:
  1. Cadastra / lista / remove devices IoT         (IoT Agent, porta 4041)
  2. Mostra gráficos em tempo real do histórico    (STH-Comet, porta 8666)
  3. Compara as leituras com os triggers e liga/desliga os alertas do ESP32
     (comandos enviados ao Orion, porta 1026 -> IoT Agent -> MQTT -> ESP32)

Arquivo único: o visual (CSS) está dentro deste próprio arquivo.

Como rodar:
  pip install -r requirements.txt
  python app.py        (abrir http://localhost:8050)
"""

import requests
import os
import plotly.graph_objects as go
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from dash import Dash, dcc, html, Input, Output, State, ctx, no_update

# ============================ CONFIGURAÇÕES ============================
IP_FIWARE = "54.242.88.65"        # IP da máquina onde o FIWARE está rodando
PORTA_IOTA = 4041                  # IoT Agent (cadastro de devices)
PORTA_ORION = 1026                 # Orion Context Broker (comandos e subscriptions)
PORTA_STH = 8666                   # STH-Comet (dados históricos)

FIWARE_SERVICE = "smart"           # cabeçalhos padrão do FIWARE Descomplicado
FIWARE_SERVICEPATH = "/"
APIKEY = "TEF"                     # mesma apikey dos tópicos MQTT (/TEF/...)
TIPO_ENTIDADE = "Vinheria"

# Configure estes valores no ambiente antes de iniciar o dashboard.
# Nunca coloque o token do bot diretamente no código ou em repositório público.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Nomes dos containers vistos "de dentro" do Docker (ajuste se forem diferentes)
CBROKER_INTERNO = "http://orion:1026"
STH_INTERNO = "http://sth-comet:8666"

INTERVALO_MS = 5000                # atualização automática (5 s)
PONTOS = 30                        # pontos mostrados em cada gráfico (editável na tela)

# Atributos do device. A chave é o nome no Orion; "comando" é o comando que o
# firmware do ESP32 entende. "cor" é a cor da linha do gráfico. min/max são os
# triggers iniciais (editáveis na tela).
PARAMETROS = {
    "temperature": {"nome": "Temperatura",  "unidade": "°C", "comando": "alertaTemp", "cor": "#d4af37", "min": 12, "max": 18},
    "humidity":    {"nome": "Umidade",      "unidade": "%",  "comando": "alertaUmid", "cor": "#cd7f32", "min": 50, "max": 70},
    "luminosity":  {"nome": "Luminosidade", "unidade": "%",  "comando": "alertaLuz", "cor": "#f3e5ab",  "min": 0,  "max": 30},
}
# =======================================================================

HEADERS = {"fiware-service": FIWARE_SERVICE, "fiware-servicepath": FIWARE_SERVICEPATH}
HEADERS_JSON = {**HEADERS, "Content-Type": "application/json"}

# Guarda quais alertas estão ligados: {(device_id, atributo): True/False}.
# Assim o comando só é enviado quando o estado MUDA (normal -> anomalia ou o contrário).
estado_alertas = {}
# Último estado notificado com sucesso, separado do estado dos comandos do ESP32.
estado_notificacoes = {}


# ============================ FUNÇÕES FIWARE ============================

def url(porta, caminho):
    return f"http://54.242.88.65:{porta}{caminho}"


def entidade_do(device_id):
    # Nome da entidade no Orion, ex.: urn:ngsi-ld:Vinheria:InVino
    return f"urn:ngsi-ld:{TIPO_ENTIDADE}:{device_id}"


def chamar_api(metodo, endereco, corpo=None, espera=(3, 5)):
    """Único ponto que faz requisições HTTP. Devolve (resposta, erro).
    O try/except fica só aqui: se o FIWARE cair, o dashboard não quebra."""
    headers = HEADERS_JSON if corpo is not None else HEADERS
    try:
        resposta = requests.request(metodo, endereco, headers=headers, json=corpo, timeout=espera)
        return resposta, None
    except requests.RequestException as erro:
        return None, str(erro)


def listar_devices():
    resposta, erro = chamar_api("GET", url(PORTA_IOTA, "/iot/devices"))
    if erro or not resposta.ok:
        return []
    return resposta.json().get("devices", [])


def cadastrar_device(device_id):
    """Cadastra o device no IoT Agent e cria a subscription que alimenta o STH-Comet."""
    entidade = entidade_do(device_id)

    # 1) Service group da apikey (se já existir o IoT Agent responde 409, sem problema)
    grupo = {"services": [{"apikey": APIKEY, "cbroker": CBROKER_INTERNO,
                           "entity_type": TIPO_ENTIDADE, "resource": ""}]}
    _, erro = chamar_api("POST", url(PORTA_IOTA, "/iot/services"), grupo)
    if erro:
        return (f"Sem conexão com o IoT Agent ({IP_FIWARE}:{PORTA_IOTA}). "
                "Confira o IP e se a porta está liberada no firewall.")

    # 2) Device: traduz t/h/l (enviados pelo ESP32) para temperature/humidity/luminosity
    #    e registra os 3 comandos de alerta
    device = {"devices": [{
        "device_id": device_id,
        "entity_name": entidade,
        "entity_type": TIPO_ENTIDADE,
        "protocol": "PDI-IoTA-UltraLight",
        "transport": "MQTT",
        "commands": [{"name": p["comando"], "type": "command"} for p in PARAMETROS.values()],
        "attributes": [
            {"object_id": "t", "name": "temperature", "type": "Number"},
            {"object_id": "h", "name": "humidity", "type": "Number"},
            {"object_id": "l", "name": "luminosity", "type": "Number"},
        ],
    }]}
    resposta, erro = chamar_api("POST", url(PORTA_IOTA, "/iot/devices"), device)
    if erro:
        return f"Sem conexão com o IoT Agent: {erro}"
    if resposta.status_code == 409:
        return f"O device '{device_id}' já existe."
    if not resposta.ok:
        return f"Erro ao cadastrar ({resposta.status_code}): {resposta.text}"

    # 3) Subscription: o Orion avisa o STH-Comet a cada medição nova (é isso que gera o histórico)
    atributos = list(PARAMETROS.keys())
    subscription = {
        "description": f"Historico STH-Comet de {entidade}",
        "subject": {"entities": [{"id": entidade, "type": TIPO_ENTIDADE}],
                    "condition": {"attrs": atributos}},
        "notification": {"http": {"url": f"{STH_INTERNO}/notify"},
                         "attrs": atributos, "attrsFormat": "legacy"},
        "throttling": 1,
    }
    resposta, erro = chamar_api("POST", url(PORTA_ORION, "/v2/subscriptions"), subscription)
    if erro or not resposta.ok:
        return f"Device cadastrado, mas a subscription do STH-Comet falhou: {erro or resposta.text}"
    return f"Device '{device_id}' cadastrado com sucesso."


def remover_device(device_id):
    if not device_id:
        return "Selecione um device para remover."
    resposta, erro = chamar_api("DELETE", url(PORTA_IOTA, f"/iot/devices/{device_id}"))
    if erro:
        return f"Sem conexão: {erro}"
    for chave in [c for c in estado_alertas if c[0] == device_id]:
        del estado_alertas[chave]
    for chave in [c for c in estado_notificacoes if c[0] == device_id]:
        del estado_notificacoes[chave]
    return f"Device '{device_id}' removido." if resposta.ok else f"Erro ao remover: {resposta.text}"


def ler_historico(device_id, atributo, quantidade):
    """Lê os últimos N valores do atributo no STH-Comet. Devolve (horas, valores)."""
    caminho = (f"/STH/v1/contextEntities/type/{TIPO_ENTIDADE}/id/{entidade_do(device_id)}"
               f"/attributes/{atributo}?lastN={quantidade}")
    resposta, erro = chamar_api("GET", url(PORTA_STH, caminho))
    if erro or not resposta.ok:
        return [], []

    valores_sth = resposta.json()["contextResponses"][0]["contextElement"]["attributes"][0]["values"]
    horas, valores = [], []
    for item in valores_sth:
        # recvTime vem em UTC (ex.: 2026-10-26T19:00:00.000Z); converte para Brasília (UTC-3)
        hora_utc = datetime.fromisoformat(item["recvTime"].replace("Z", "+00:00"))
        horas.append(hora_utc + timedelta(hours=-3))
        valores.append(float(item["attrValue"]))
    return horas, valores


def enviar_comando(device_id, comando, valor):
    """Manda o comando (on/off) pelo Orion; o IoT Agent entrega ao ESP32 via MQTT."""
    corpo = {comando: {"type": "command", "value": valor}}
    resposta, erro = chamar_api("PATCH", url(PORTA_ORION, f"/v2/entities/{entidade_do(device_id)}/attrs"), corpo)
    return erro is None and resposta.ok


def enviar_telegram(mensagem):
    """Envia uma mensagem pelo bot. Retorna False se faltar configuração ou houver erro."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    endereco = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resposta = requests.post(endereco, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensagem}, timeout=(3, 8))
        return resposta.ok and resposta.json().get("ok", False)
    except (requests.RequestException, ValueError):
        return False


def notificar_mudanca(device_id, atributo, p, atual, minimo, maximo, fora):
    """Notifica entrada em anomalia e retorno ao normal; falhas são tentadas no próximo ciclo."""
    chave = (device_id, atributo)
    estado_anterior = estado_notificacoes.get(chave)
    if estado_anterior is None:
        # Não envia mensagem de normal ao iniciar. Se já começar fora do limite, avisa.
        if not fora:
            estado_notificacoes[chave] = False
            return
        texto = (f"🚨 InVino | {device_id} | {datetime.now():%d/%m/%Y %H:%M:%S}\n"
                 f"{p['nome']} fora do limite: {atual:.1f} {p['unidade']} "
                 f"(mín. {minimo if minimo is not None else '—'}, máx. {maximo if maximo is not None else '—'}).")
    elif estado_anterior == fora:
        return
    elif fora:
        texto = (f"🚨 InVino | {device_id} | {datetime.now():%d/%m/%Y %H:%M:%S}\n"
                 f"{p['nome']} fora do limite: {atual:.1f} {p['unidade']} "
                 f"(mín. {minimo if minimo is not None else '—'}, máx. {maximo if maximo is not None else '—'}).")
    else:
        texto = (f"✅ InVino | {device_id} | {datetime.now():%d/%m/%Y %H:%M:%S}\n"
                 f"{p['nome']} voltou ao normal: {atual:.1f} {p['unidade']}.")
    if enviar_telegram(texto):
        estado_notificacoes[chave] = fora


def servico_online(porta, caminho):
    resposta, erro = chamar_api("GET", url(porta, caminho), espera=(2, 3))
    return erro is None and resposta.ok


# ============================ GRÁFICOS ============================

ALERTA = "#e5484d"


def montar_figura(p, horas, valores, minimo, maximo):
    figura = go.Figure(go.Scatter(x=horas, y=valores, mode="lines+markers", name=p["nome"],
                                  line=dict(color=p["cor"], width=2), marker=dict(size=6)))
    # linhas tracejadas = triggers
    if minimo is not None:
        figura.add_hline(y=minimo, line_dash="dash", line_color=ALERTA, annotation_text="mín")
    if maximo is not None:
        figura.add_hline(y=maximo, line_dash="dash", line_color=ALERTA, annotation_text="máx")
    figura.update_layout(
        title=dict(text=f"{p['nome']} ({p['unidade']})", font=dict(color=p["cor"])),
        height=300, margin=dict(l=40, r=20, t=50, b=40),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#8d866f"), uirevision="fixo",  # uirevision mantém o zoom entre atualizações
        xaxis=dict(gridcolor="#1f1b0e", linecolor="#2b2615"),
        yaxis=dict(gridcolor="#1f1b0e", linecolor="#2b2615"),
    )
    return figura


# ============================ VISUAL (CSS) ============================

CSS = """
:root{--ouro:#d4af37;--ouro-claro:#f0d77a;--fundo:#0a0a0a;--painel:#141414;--borda:#2b2615;
      --texto:#ece6d3;--mudo:#8d866f;--alerta:#e5484d}
body{margin:0;background:var(--fundo);color:var(--texto);font-family:'Segoe UI',system-ui,sans-serif}
.pagina{max-width:1100px;margin:0 auto;padding:32px 20px 60px}
.topo{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:16px;
      border-bottom:1px solid var(--borda);padding-bottom:20px}
h1{margin:0;color:var(--ouro);letter-spacing:.35em;font-weight:300;font-size:28px}
.topo p{margin:4px 0 0;color:var(--mudo);font-size:13px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:12px;padding:4px 10px;border:1px solid var(--borda);border-radius:999px;color:var(--mudo)}
.chip::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;background:var(--alerta)}
.chip.on{color:var(--texto)}
.chip.on::before{background:var(--ouro)}
h2{font-size:13px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:var(--ouro);margin:36px 0 14px}
.linha{display:flex;gap:10px;flex-wrap:wrap}
.campo{background:var(--painel);border:1px solid var(--borda);color:var(--texto);padding:9px 12px;border-radius:6px;outline:none}
.campo:focus{border-color:var(--ouro)}
.campo.mini{width:60px;padding:5px 8px;margin:0 12px 0 6px}
.btn{background:var(--ouro);color:#0a0a0a;border:1px solid var(--ouro);padding:9px 18px;border-radius:6px;font-weight:600;cursor:pointer}
.btn:hover{background:var(--ouro-claro)}
.btn.ghost{background:transparent;color:var(--ouro)}
.btn.ghost:hover{background:#1c1808}
.msg{margin:10px 0;color:var(--mudo);font-size:14px;min-height:20px}
.devices{display:flex;gap:8px;flex-wrap:wrap}
.pill{border:1px solid var(--borda);border-radius:999px;padding:6px 16px;cursor:pointer;color:var(--mudo);font-size:14px}
.pill-input{display:none}
.devices .dash-options-list-option{display:inline-flex;border:1px solid var(--borda);border-radius:999px;padding:6px 16px;
      cursor:pointer;color:var(--mudo);font-size:14px;margin:0}
.devices .dash-options-list-option.selected{border-color:var(--ouro);color:var(--ouro);background:#14110a}
.devices .dash-options-list-option:hover{border-color:var(--ouro)}
.devices .dash-options-list-option-wrapper{display:none}
.devices .pill{border:0;padding:0;background:transparent;color:inherit}
.pill:has(.pill-input:checked){border-color:var(--ouro);color:var(--ouro);background:#14110a}
.grade{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{background:var(--painel);border:1px solid var(--borda);border-radius:10px;padding:18px}
.card-titulo{color:var(--mudo);font-size:13px;letter-spacing:.1em;text-transform:uppercase}
.card-valor{font-size:38px;font-weight:300;color:var(--ouro);margin:6px 0 2px}
.card-estado{font-size:13px;color:var(--mudo)}
.card.alerta{border-color:var(--alerta)}
.card.alerta .card-valor,.card.alerta .card-estado{color:var(--alerta)}
.limites{margin-top:14px;padding-top:12px;border-top:1px solid var(--borda);color:var(--mudo);font-size:13px;
         display:flex;align-items:center}
.painel-grafico{background:var(--painel);border:1px solid var(--borda);border-radius:10px;padding:4px}
.pilha{display:flex;flex-direction:column;gap:16px}
.titulo-linha{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--mudo);font-size:13px}
"""


# ============================ LAYOUT ============================

app = Dash(__name__)
app.title = "InVino | Monitoramento"

# Injeta o CSS dentro da página (por isso não precisa de pasta "assets")
app.index_string = """<!DOCTYPE html>
<html>
<head>
{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
<style>/*ESTILO*/</style>
</head>
<body>
{%app_entry%}
<footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>""".replace("/*ESTILO*/", CSS)


def cartao(atributo, p):
    """Cartão de um parâmetro: valor atual, estado e campos de trigger (mín/máx)."""
    return html.Div(id=f"card-{atributo}", className="card", children=[
        html.Div(p["nome"], className="card-titulo"),
        html.Div("--", id=f"valor-{atributo}", className="card-valor"),
        html.Div("sem dados", id=f"estado-{atributo}", className="card-estado"),
        html.Div(className="limites", children=[
            "mín", dcc.Input(id=f"min-{atributo}", type="number", value=p["min"], className="campo mini"),
            "máx", dcc.Input(id=f"max-{atributo}", type="number", value=p["max"], className="campo mini"),
        ]),
    ])


app.layout = html.Div(className="pagina", children=[
    dcc.Interval(id="intervalo", interval=INTERVALO_MS, n_intervals=0),

    html.Div(className="topo", children=[
        html.Div([html.H1("InVino"), html.P("Monitoramento de vinherias em tempo real")]),
        html.Div(id="chips", className="chips"),
    ]),

    html.H2("Devices"),
    html.Div(className="linha", children=[
        dcc.Input(id="in-device", type="text", value="InVino", placeholder="ID do device (ex.: InVino)", className="campo"),
        html.Button("Cadastrar", id="btn-cadastrar", n_clicks=0, className="btn"),
        html.Button("Remover selecionado", id="btn-remover", n_clicks=0, className="btn ghost"),
    ]),
    dcc.Loading(type="dot", color="#d4af37", children=html.Div(id="msg", className="msg")),
    dcc.RadioItems(id="sel-device", options=[], className="devices",
                   labelClassName="pill", inputClassName="pill-input"),

    html.Div(className="titulo-linha", children=[
        html.H2("Situação atual"),
        html.Div(id="selecionado"),
    ]),
    html.Div(className="grade", children=[cartao(a, p) for a, p in PARAMETROS.items()]),

    html.Div(className="titulo-linha", children=[
        html.H2("Histórico"),
        html.Div(["Pontos no gráfico ", dcc.Input(id="qtd-pontos", type="number", value=PONTOS,
                                                  min=5, max=200, className="campo mini")]),
    ]),
    html.Div(className="pilha", children=[
        dcc.Graph(id=f"grafico-{a}", className="painel-grafico") for a in PARAMETROS]),
])


# ============================ CALLBACKS ============================

@app.callback(Output("chips", "children"), Input("intervalo", "n_intervals"))
def atualizar_status_servicos(_intervalo):
    """Bolinhas no topo: mostram se cada serviço do FIWARE está respondendo (testados em paralelo)."""
    servicos = [("IoT Agent", PORTA_IOTA, "/iot/about"),
                ("Orion", PORTA_ORION, "/version"),
                ("STH-Comet", PORTA_STH, "/version")]
    with ThreadPoolExecutor(max_workers=3) as executor:
        online = list(executor.map(lambda servico: servico_online(servico[1], servico[2]), servicos))
    chips = [html.Span(servico[0], className="chip on" if ok else "chip")
             for servico, ok in zip(servicos, online)]
    telegram_ok = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    chips.append(html.Span("Telegram configurado" if telegram_ok else "Telegram sem configuração",
                           className="chip on" if telegram_ok else "chip"))
    return chips


@app.callback(
    Output("msg", "children"),
    Input("btn-cadastrar", "n_clicks"), Input("btn-remover", "n_clicks"),
    State("in-device", "value"), State("sel-device", "value"),
    prevent_initial_call=True,
)
def acao_device(_cadastrar, _remover, novo_id, selecionado):
    """Roda só quando um botão é clicado: cadastra ou remove e devolve a mensagem do resultado."""
    if ctx.triggered_id == "btn-cadastrar":
        novo_id = (novo_id or "").strip()
        if not novo_id.isalnum():
            return "Use só letras e números no ID (ex.: InVino)."
        return cadastrar_device(novo_id)
    return remover_device(selecionado)


@app.callback(
    Output("sel-device", "options"), Output("sel-device", "value"),
    Input("intervalo", "n_intervals"), Input("msg", "children"),
    State("sel-device", "value"),
)
def atualizar_lista(_intervalo, _mensagem, selecionado):
    """Atualiza a lista de devices a cada 5 s e logo depois de cadastrar/remover."""
    ids = [d["device_id"] for d in listar_devices()]
    # mantém o device escolhido; se ele sumiu (ou nada foi escolhido), seleciona o primeiro da lista
    valor = no_update if selecionado in ids else (ids[0] if ids else None)
    return ids, valor


# Entradas e saídas do painel (montadas em loop para os 3 parâmetros)
entradas = [Input("intervalo", "n_intervals"), Input("sel-device", "value"), Input("qtd-pontos", "value")]
saidas = []
for _atributo in PARAMETROS:
    entradas += [Input(f"min-{_atributo}", "value"), Input(f"max-{_atributo}", "value")]
    saidas += [Output(f"grafico-{_atributo}", "figure"), Output(f"card-{_atributo}", "className"),
               Output(f"valor-{_atributo}", "children"), Output(f"estado-{_atributo}", "children")]


saidas.append(Output("selecionado", "children"))


@app.callback(saidas, entradas)
def atualizar_painel(_intervalo, device_id, quantidade, *limites):
    """A cada 5 s: lê o histórico, atualiza gráficos/cartões e liga/desliga alertas no ESP32."""
    quantidade = quantidade or PONTOS
    resultado = []

    for i, (atributo, p) in enumerate(PARAMETROS.items()):
        minimo, maximo = limites[2 * i], limites[2 * i + 1]
        horas, valores = ler_historico(device_id, atributo, quantidade) if device_id else ([], [])
        figura = montar_figura(p, horas, valores, minimo, maximo)

        if not valores:
            resultado += [figura, "card", "--",
                          "aguardando medições" if device_id else "selecione um device"]
            continue

        atual = valores[-1]
        fora = (minimo is not None and atual < minimo) or (maximo is not None and atual > maximo)

        notificar_mudanca(device_id, atributo, p, atual, minimo, maximo, fora)

        # Só envia comando quando o estado muda: normal -> anomalia (on) ou anomalia -> normal (off)
        chave = (device_id, atributo)
        ligado = estado_alertas.get(chave, False)
        if fora and not ligado:
            if enviar_comando(device_id, p["comando"], "on"):
                estado_alertas[chave] = True
        elif not fora and ligado:
            if enviar_comando(device_id, p["comando"], "off"):
                estado_alertas[chave] = False

        resultado += [figura, "card alerta" if fora else "card",
                      f"{atual:.1f} {p['unidade']}", "Fora do limite" if fora else "Normal"]

    resultado.append(f"Monitorando: {device_id}" if device_id else "Nenhum device selecionado")
    return resultado


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8050, debug=False)
