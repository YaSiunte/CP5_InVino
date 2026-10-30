# InVino — Monitoramento de vinherias

Dashboard web para acompanhar temperatura, umidade e luminosidade de dispositivos IoT usados no monitoramento de uma vinheria. O projeto integra ESP32, FIWARE, MQTT, gráficos interativos e notificações pelo Telegram.

> Projeto acadêmico. O dashboard roda localmente durante a demonstração e depende dos serviços FIWARE configurados no código.

## Funcionalidades

- Cadastro, listagem e remoção de dispositivos no IoT Agent.
- Acompanhamento de temperatura, umidade e luminosidade.
- Cartões de status e gráficos de histórico com atualização automática a cada 5 segundos.
- Ajuste de limites mínimo e máximo diretamente no dashboard.
- Envio de comandos `on`/`off` ao dispositivo quando uma leitura muda de estado.
- Notificações pelo Telegram quando uma leitura sai dos limites e quando retorna à faixa.
- Suporte a vários dispositivos, desde que cada um esteja cadastrado com o mesmo ID configurado no firmware.

## Simulação no Wokwi

A simulação do circuito InVino pode ser acessada pelo link:

[ abrir simulação no Wokwi](https://wokwi.com/projects/478992223348248577)

O projeto no Wokwi contém a montagem do ESP32, os sensores e os componentes de sinalização.

## Tecnologias

- Python 3
- Dash
- Plotly
- Requests
- FIWARE IoT Agent, Orion Context Broker e STH-Comet
- ESP32 comunicando-se por MQTT com mensagens Ultralight

## Firmware ESP32

O sketch configura o ESP32 para ler o DHT22 e o LDR, enviar dados por MQTT, receber comandos e indicar alertas no LCD, LED RGB, LED azul e buzzer. O ID padrão do dispositivo é InVino.

Para usar uma placa física, revise no início de sketch.ino o SSID e a senha do Wi-Fi, o endereço do broker MQTT e o tipo de sensor DHT. O diagrama e as bibliotecas necessárias estão descritos no próprio sketch e na simulação Wokwi.

## Requisitos

- Python 3 instalado.
- Acesso de rede aos serviços FIWARE usados pelo projeto:
  - IoT Agent: porta `4041`
  - Orion: porta `1026`
  - STH-Comet: porta `8666`
- Um dispositivo ESP32 configurado para publicar os dados no broker MQTT esperado pelo ambiente FIWARE (porta `1883`).
- Credenciais do bot e o ID do chat do Telegram, caso queira habilitar as notificações.

O firmware está no arquivo `sketch.ino`. O dispositivo precisa usar o mesmo ID cadastrado no dashboard e publicar as medições no formato Ultralight esperado pelo projeto.

## Instalação e execução

Abra o PowerShell na pasta do projeto e instale as dependências:

```powershell
python -m pip install -r requirements.txt
```

Configure as variáveis do Telegram na mesma janela do PowerShell. Não coloque o token diretamente no código nem o envie ao GitHub:

```powershell
$env:TELEGRAM_BOT_TOKEN = "TOKEN_DO_BOT"
$env:TELEGRAM_CHAT_ID = "ID_DO_CHAT"
```

Inicie o dashboard:

```powershell
python app.py
```

No navegador do mesmo computador, acesse:

```text
http://127.0.0.1:8050/
```

Mantenha o PowerShell aberto enquanto estiver usando o dashboard. Para encerrar, pressione `Ctrl+C` nessa janela. As variáveis do Telegram precisam ser definidas novamente ao abrir uma nova janela.

## Cadastrar um dispositivo

1. Confirme que o ESP32 e os serviços FIWARE estão configurados e acessíveis.
2. No campo de ID do dispositivo, informe o ID usado pelo firmware. Para o sketch InVino, use `InVino`.
3. Clique em **Cadastrar**.
4. Selecione o dispositivo para acompanhar suas medições.

O dashboard usa a API do IoT Agent para cadastrar o dispositivo e cria uma assinatura no Orion para alimentar o histórico consultado pelo STH-Comet. Se o cadastro falhar, verifique o IP do FIWARE, a disponibilidade dos serviços e as regras de rede/firewall.

## Limites e alertas

Os limites iniciais definidos no código são:

| Parâmetro | Mínimo | Máximo |
| --- | ---: | ---: |
| Temperatura | 12 °C | 18 °C |
| Umidade | 50% | 70% |
| Luminosidade | 0% | 30% |

Os valores podem ser alterados na tela. As alterações ficam em memória e retornam aos padrões quando o dashboard é reiniciado. O projeto apresenta os estados normal e fora do limite; não há níveis distintos de alerta e crítico.

A luminosidade é apresentada como percentual calculado a partir da leitura ADC do LDR, e não como lux calibrado. Os limites são demonstrativos e não representam recomendações técnicas para armazenamento de vinhos.

## Configuração do FIWARE

O endereço IP e as portas dos serviços estão definidos no início de `app.py`. Se o endereço do servidor mudar, atualize as configurações correspondentes e reinicie o dashboard. O broker e as configurações de MQTT do ESP32 também precisam apontar para o ambiente FIWARE correto.

O dashboard precisa conseguir acessar os endpoints HTTP do IoT Agent, Orion e STH-Comet. Um teste de conexão TCP à porta, isoladamente, não garante que uma política de rede permita as requisições HTTP.

## Segurança

- Nunca publique token do Telegram, senhas de Wi-Fi ou outras credenciais.
- O dashboard atual não possui autenticação e escuta localmente; `127.0.0.1` só é acessível no próprio computador.
- Não exponha as portas dos serviços FIWARE à internet sem as proteções apropriadas.

## Arquivos

- `app.py` — aplicação Dash, integração com FIWARE, gráficos, limites e notificações.
- `requirements.txt` — dependências Python.
- `sketch.ino` — firmware do ESP32.
- `Manual_do_Usuario_InVino_Cliente.docx` — manual de uso em linguagem voltada ao usuário.

