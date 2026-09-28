# MySoul na placa

Assistente de voz na Cheap Yellow Display (ESP32-2432S028R). A primeira tela é um orb: um toque ouve e responde. A resposta sai no computador, no Bluetooth e nos aparelhos de áudio ligados nele. O texto vem do modelo local `chaoliangUNSW/Jev-Style-Qwen3_5-2B-Decision-Q4_K_M`, no Atomic Chat, usando os arquivos do projeto MySoul.

A segunda página é a configuração, no navegador, pelo IP da placa: `http://<ip-da-placa>/`. Dá para adicionar, testar e apagar servidores MCP. O mesmo painel abre em `http://127.0.0.1:8791/` no computador.

## Outra máquina

1. Instale Python 3.11 ou mais novo e o suporte ESP32 no Arduino.
2. Na pasta do projeto:

```powershell
python -m pip install -r gateway\requirements.txt
copy firmware\mysoul_cyd\secrets.h.example firmware\mysoul_cyd\secrets.h
```

3. Edite `secrets.h` com o Wi-Fi desta casa. `PC_HOST` é o IPv4 deste computador, ou deixe vazio para a placa achar o PC pelo UDP `8792`.
4. Abra o Atomic Chat com o modelo `chaoliangUNSW/Jev-Style-Qwen3_5-2B-Decision-Q4_K_M` em `127.0.0.1:1337`, e com o projeto MySoul importado. Se houver mais de um projeto, aponte o banco:

```powershell
$env:MYSOUL_DB = "$env:APPDATA\Atomic Chat\data\db\project_SEU_ID.db"
$env:MYSOUL_COM = "COM5"
cd gateway
python -u server.py
```

O cabo USB da placa é a ligação confiável. `MYSOUL_COM` é a porta do CH340. Na primeira execução o gateway cria `gateway/webhook.json` com um token novo. A chave do Jev, se for usar as leituras, fica numa linha em `gateway/jev.secret`. Nenhum desses arquivos entra no Git.

Para o agente mostrar um texto na placa sem passar pelo modelo:

```json
{"type": "display", "text": "Bom dia.", "speak": false}
```

`"raw": true` faz o mesmo. `"speak": true` lê essa frase no alto-falante do PC.

## Gravar a placa

Bibliotecas em `firmware/libraries`: Adafruit GFX, ILI9341, BusIO e XPT2046. Segure o botão BOOT durante a gravação. O reset da placa é pelo cabo USB.

```powershell
arduino-cli compile --fqbn esp32:esp32:esp32 --libraries firmware/libraries firmware/mysoul_cyd
arduino-cli upload -p COMx --fqbn esp32:esp32:esp32 --libraries firmware/libraries firmware/mysoul_cyd
```

## O que a placa faz

- Toque no orb: o computador apita, ouve e fala a resposta.
- Microfone: Bluetooth primeiro, senão o do computador, senão outro aparelho ligado.
- Voz: sai ao mesmo tempo em cada saída real (alto-falante, fone, Bluetooth).
- A página da placa só configura. Quem executa os MCP é o gateway neste computador.

Um comando MCP roda aqui. A página não tem senha e deve ficar na rede local.

## Agente orquestrador

O Grokbot fala com o agente `orquestrador` por `POST /hooks/grokbot`. O token é criado em `gateway/webhook.json` na primeira execução e também aparece na página de configuração.

Cabeçalho: `Authorization: Bearer <token>`.

```json
{"message": "o que tenho para hoje"}
```

Para falar com outro agente já ligado na página, envie `"to": "nome"`. Para falar com todos, `"broadcast": true`. `"speak": true` também lê a resposta em voz alta. Um ping é `{"type": "ping"}` e responde `pong`.
