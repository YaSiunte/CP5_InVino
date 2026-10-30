/*
  CP5 - Edge Computing
  Monitoramento de vinherias com ESP32 + DHT + LDR + Buzzer + LED azul

  Fluxo:
    ESP32 --(MQTT, Ultralight)--> IoT Agent --> Orion --> STH-Comet (8666) --> Dashboard
    Dashboard --> Orion --> IoT Agent --(MQTT, comando)--> ESP32

  O ESP32 NÃO decide sozinho quando alertar: o dashboard compara os valores
  com os triggers e envia os comandos abaixo (on = liga alerta, off = desliga):
    InVino@alertaTemp|on / off   -> anomalia de temperatura
    InVino@alertaUmid|on / off   -> anomalia de umidade
    InVino@alertaLuz|on  / off   -> anomalia de luminosidade

  Enquanto QUALQUER alerta estiver ligado, o LED azul fica piscando.
  O LED RGB mostra QUAL é o problema: vermelho = temperatura, azul = umidade,
  amarelo = luminosidade (com mais de um alerta, as cores se alternam).
  Cada tipo de anomalia toca um som diferente no buzzer.

  Teste sem FIWARE: no Monitor Serial digite, por exemplo, alertaTemp|on
*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ===================== CONFIGURAÇÕES (ajuste aqui) =====================
const char* SSID     = "Wokwi-GUEST";   // no laboratório, troque pela rede real
const char* PASSWORD = "";

const char* BROKER_MQTT = "34.228.230.88";  // IP da VM onde roda o FIWARE
const int   BROKER_PORT = 1883;

const char* ID_DEVICE    = "InVino";
const char* TOPIC_ATTRS  = "/TEF/InVino/attrs";   // ESP32 publica medições
const char* TOPIC_CMD    = "/TEF/InVino/cmd";     // ESP32 recebe comandos
const char* TOPIC_CMDEXE = "/TEF/InVino/cmdexe";  // ESP32 confirma comandos

// No Wokwi o sensor é DHT22. No hardware real (DHT-11) troque para DHT11.
#define DHTTYPE DHT22

// Pinos
const int DHT_PIN    = 15;  // dados do DHT
const int LDR_PIN    = 34;  // entrada analógica (ADC1, não conflita com Wi-Fi)
const int BUZZER_PIN = 18;
const int RGB_R      = 25;  // LED RGB: pino do vermelho
const int RGB_G      = 26;  // LED RGB: pino do verde
const int RGB_B      = 27;  // LED RGB: pino do azul
const int LED_AZUL   = 2;   // LED azul EXTERNO (resistor + LED); o onboard também pisca junto

// Se no seu circuito real mais luz = menor leitura, coloque true
const bool LDR_INVERTIDO = false;

// LED RGB: false = cátodo comum (perna maior no GND); true = ânodo comum (perna maior no 3V3)
const bool RGB_ANODO_COMUM = false;

// Cor de cada alerta {R, G, B}: 1 = ligado, 0 = desligado
//                              temperatura   umidade     luminosidade
const bool COR_ALERTA[3][3] = { { 1, 0, 0 }, { 0, 0, 1 }, { 1, 1, 0 } };
//                              vermelho      azul        amarelo
const unsigned long INTERVALO_COR = 700;  // tempo de cada cor quando há mais de um alerta

// Display LCD 16x2 com módulo I2C (SDA = GPIO 21, SCL = GPIO 22).
// Se o display ficar sem texto, o endereço pode ser 0x3F em vez de 0x27.
const uint8_t LCD_ENDERECO = 0x27;

// Intervalos (ms)
const unsigned long INTERVALO_ENVIO = 5000;
const unsigned long INTERVALO_PISCA = 300;
const unsigned long INTERVALO_RECONEXAO = 5000;
// =======================================================================

DHT dht(DHT_PIN, DHTTYPE);
WiFiClient espClient;
PubSubClient mqtt(espClient);
LiquidCrystal_I2C lcd(LCD_ENDERECO, 16, 2);

// Estado dos alertas: 0 = temperatura, 1 = umidade, 2 = luminosidade
bool alerta[3] = { false, false, false };

// ----- Padrões sonoros (frequência em Hz, duração em ms; freq 0 = silêncio) -----
struct Passo {
  int freq;
  int ms;
};

// Temperatura: 1 bipe longo e grave
const Passo SOM_TEMP[] = { { 880, 700 } };
// Umidade: 2 bipes curtos e agudos
const Passo SOM_UMID[] = { { 1500, 150 }, { 0, 150 }, { 1500, 150 } };
// Luminosidade: 3 bipes curtos e médios
const Passo SOM_LUZ[] = { { 600, 150 }, { 0, 150 }, { 600, 150 }, { 0, 150 }, { 600, 150 } };

const Passo* padroes[3] = { SOM_TEMP, SOM_UMID, SOM_LUZ };
const int tamanhos[3]   = { 1, 3, 5 };

// Controle do tocador de sons (sem delay, para não travar o resto do código)
bool tocando = false;
int alertaTocando = 0;
int passoAtual = 0;
unsigned long inicioPasso = 0;
unsigned long proximoSom = 0;
int ultimoTocado = 2;

// Controle de tempo
unsigned long ultimoEnvio = 0;
unsigned long ultimaPisca = 0;
unsigned long ultimaReconexao = 0;
bool ledLigado = false;
int corIndice = 0;                  // qual alerta está sendo mostrado no RGB
unsigned long ultimaTrocaCor = 0;

// Últimos valores lidos (mostrados no display)
float ultimaTemp = NAN;
float ultimaUmid = NAN;
int ultimaLuz = 0;

// Símbolos personalizados dos sensores (um caractere do LCD cada).
byte simboloTemperatura[8] = {
  B00100,
  B00100,
  B00100,
  B00100,
  B00100,
  B01110,
  B01110,
  B00100
};

byte simboloUmidade[8] = {
  B00100,
  B00100,
  B01010,
  B01010,
  B10001,
  B10001,
  B01010,
  B00100
};

byte simboloLuminosidade[8] = {
  B00100,
  B10101,
  B01110,
  B11111,
  B01110,
  B10101,
  B00100,
  B00000
};


// ----------------------------- Funções -----------------------------

bool algumAlerta() {
  return alerta[0] || alerta[1] || alerta[2];
}

// Completa com espaços até 16 colunas (apaga restos da escrita anterior)
String linha16(String texto) {
  while (texto.length() < 16) texto += " ";
  return texto.substring(0, 16);
}

// LCD 16x2: linha 1 = temperatura e umidade; linha 2 = luminosidade e alertas
// Exemplo: "T:24.5C H:60%"  /  "L:40% ALR:TH" (T=temp, H=umidade, L=luz)
void atualizarDisplay() {
  // Limpa as linhas para remover possíveis sobras de valores anteriores.
  lcd.setCursor(0, 0);
  lcd.print("                ");
  lcd.setCursor(0, 1);
  lcd.print("                ");

  // Linha 1: temperatura e umidade, identificadas pelos símbolos.
  lcd.setCursor(0, 0);
  if (isnan(ultimaTemp) || isnan(ultimaUmid)) {
    lcd.write((byte)0);
    lcd.print(":Erro no DHT");
  } else {
    lcd.write((byte)0);
    lcd.print(":");
    lcd.print(String(ultimaTemp, 1));
    lcd.print("C ");

    lcd.write((byte)1);
    lcd.print(":");
    lcd.print((int)ultimaUmid);
    lcd.print("%");
  }

  // Linha 2: luminosidade e símbolos dos alertas ativos.
  lcd.setCursor(0, 1);
  lcd.write((byte)2);
  lcd.print(":");
  lcd.print(ultimaLuz);
  lcd.print("% ");

  if (algumAlerta()) {
    lcd.print("ALR:");
    if (alerta[0]) lcd.write((byte)0);
    if (alerta[1]) lcd.write((byte)1);
    if (alerta[2]) lcd.write((byte)2);
  } else {
    lcd.print("OK");
  }
}

void conectarWiFi() {
  Serial.print("Conectando ao Wi-Fi");
  WiFi.begin(SSID, PASSWORD, 6);  // canal 6 = conexão mais rápida no Wokwi
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.println(" conectado!");
}

void conectarMQTT() {
  Serial.print("Conectando ao broker MQTT... ");
  String clientId = String("fiware_") + ID_DEVICE;
  if (mqtt.connect(clientId.c_str())) {
    Serial.println("conectado!");
    mqtt.subscribe(TOPIC_CMD);
  } else {
    Serial.print("falhou (rc=");
    Serial.print(mqtt.state());
    Serial.println("), tentando de novo em breve");
  }
}

// Processa um comando no formato "InVino@alertaTemp|on" (MQTT)
// ou "alertaTemp|on" (Monitor Serial)
void processarComando(String msg) {
  msg.trim();
  int arroba = msg.indexOf('@');
  int barra = msg.indexOf('|');
  if (barra < 0) return;

  String nome = msg.substring(arroba + 1, barra);  // se não houver '@', arroba = -1 e começa em 0
  String valor = msg.substring(barra + 1);
  valor.trim();
  bool ligar = (valor == "on");

  int indice = -1;
  if (nome == "alertaTemp") indice = 0;
  else if (nome == "alertaUmid") indice = 1;
  else if (nome == "alertaLuz") indice = 2;

  if (indice < 0) {
    Serial.println("Comando desconhecido: " + nome);
    return;
  }

  alerta[indice] = ligar;
  atualizarDisplay();
  Serial.println("Alerta " + nome + (ligar ? " LIGADO" : " DESLIGADO"));

  // Confirma a execução para o IoT Agent: "InVino@alertaTemp|ok"
  String resposta = String(ID_DEVICE) + "@" + nome + "|ok";
  mqtt.publish(TOPIC_CMDEXE, resposta.c_str());
}

// Chamada automaticamente quando chega mensagem no tópico de comando
void callbackMQTT(char* topic, byte* payload, unsigned int length) {
  String msg = "";
  for (unsigned int i = 0; i < length; i++) {
    msg += (char)payload[i];
  }
  Serial.println("Comando recebido: " + msg);
  processarComando(msg);
}

// Lê os sensores e publica no formato Ultralight: t|25.0|h|60.0|l|40
void enviarMedicoes() {
  float temperatura = dht.readTemperature();
  float umidade = dht.readHumidity();

  int leituraLDR = analogRead(LDR_PIN);  // 0 a 4095
  int luminosidade = map(leituraLDR, 0, 4095, 0, 100);
  if (LDR_INVERTIDO) luminosidade = 100 - luminosidade;

  // Guarda os últimos valores e atualiza o display (mesmo se o DHT falhar)
  ultimaTemp = temperatura;
  ultimaUmid = umidade;
  ultimaLuz = luminosidade;
  atualizarDisplay();

  if (isnan(temperatura) || isnan(umidade)) {
    Serial.println("Falha ao ler o DHT!");
    return;
  }

  String payload = "t|" + String(temperatura, 1) + "|h|" + String(umidade, 1) + "|l|" + String(luminosidade);
  Serial.println("Enviando: " + payload);

  if (mqtt.connected()) {
    mqtt.publish(TOPIC_ATTRS, payload.c_str());
  }
}

// LED azul: pisca enquanto houver qualquer alerta ativo
void atualizarLed() {
  if (!algumAlerta()) {
    ledLigado = false;
    digitalWrite(LED_AZUL, LOW);
    return;
  }
  if (millis() - ultimaPisca >= INTERVALO_PISCA) {
    ultimaPisca = millis();
    ledLigado = !ledLigado;
    digitalWrite(LED_AZUL, ledLigado ? HIGH : LOW);
  }
}

// Liga/desliga as 3 cores do LED RGB (inverte a lógica se for ânodo comum)
void definirRGB(bool r, bool g, bool b) {
  digitalWrite(RGB_R, RGB_ANODO_COMUM ? !r : r);
  digitalWrite(RGB_G, RGB_ANODO_COMUM ? !g : g);
  digitalWrite(RGB_B, RGB_ANODO_COMUM ? !b : b);
}

// LED RGB: cor do alerta ativo; com vários alertas, alterna entre as cores
void atualizarRGB() {
  if (!algumAlerta()) {
    definirRGB(false, false, false);
    return;
  }

  unsigned long agora = millis();
  bool precisaTrocar = !alerta[corIndice] || (agora - ultimaTrocaCor >= INTERVALO_COR);
  if (!precisaTrocar) return;

  // procura o próximo alerta ativo (se só um estiver ativo, volta nele mesmo)
  for (int i = 1; i <= 3; i++) {
    int candidato = (corIndice + i) % 3;
    if (alerta[candidato]) {
      corIndice = candidato;
      break;
    }
  }
  ultimaTrocaCor = agora;
  definirRGB(COR_ALERTA[corIndice][0], COR_ALERTA[corIndice][1], COR_ALERTA[corIndice][2]);
}

void iniciarPasso() {
  Passo p = padroes[alertaTocando][passoAtual];
  if (p.freq > 0) tone(BUZZER_PIN, p.freq);
  else noTone(BUZZER_PIN);
}

// Buzzer: toca o padrão de cada alerta ativo, um de cada vez, em ciclo
void atualizarSom() {
  if (!algumAlerta()) {
    if (tocando) noTone(BUZZER_PIN);
    tocando = false;
    return;
  }

  unsigned long agora = millis();

  if (!tocando) {
    if (agora < proximoSom) return;
    // procura o próximo alerta ativo para tocar
    for (int i = 0; i < 3; i++) {
      ultimoTocado = (ultimoTocado + 1) % 3;
      if (alerta[ultimoTocado]) break;
    }
    alertaTocando = ultimoTocado;
    passoAtual = 0;
    inicioPasso = agora;
    tocando = true;
    iniciarPasso();
  } else if (agora - inicioPasso >= (unsigned long)padroes[alertaTocando][passoAtual].ms) {
    passoAtual++;
    if (passoAtual >= tamanhos[alertaTocando]) {
      noTone(BUZZER_PIN);
      tocando = false;
      proximoSom = agora + 1500;  // pausa entre um som e outro
    } else {
      inicioPasso = agora;
      iniciarPasso();
    }
  }
}

// Desenho de abertura: garrafa servindo uma taça no LCD 16x2.
// Os caracteres 0-5 são fixos; o caractere 6 muda para animar o líquido.
byte desenhoC1_11[] = { B00010, B00010, B00010, B00010, B00001, B00000, B00000, B00000 };
byte desenhoF0_12[] = { B00000, B00000, B00000, B00001, B00011, B00010, B00010, B00010 };
byte desenhoC0_13[] = { B00000, B00000, B00000, B11111, B11111, B00000, B00000, B00000 };
byte desenhoC1_14[] = { B00000, B01111, B11111, B11111, B11111, B11111, B01111, B00000 };
byte desenhoC0_15[] = { B00000, B11111, B11111, B11111, B11111, B11111, B11111, B00000 };
byte desenhoC1_13[] = { B01000, B01000, B01000, B01000, B10000, B00000, B00000, B00000 };
byte liquidoFrame1[] = { B00110, B00100, B00100, B00100, B10101, B10101, B01110, B00100 };
byte liquidoFrame2[] = { B00110, B00100, B00100, B00100, B11111, B11111, B01110, B00100 };
byte liquidoFrame3[] = { B00110, B00100, B11111, B11111, B11111, B11111, B01110, B00100 };

void desenharCenarioCompleto() {
  lcd.setCursor(12, 0); lcd.write((byte)1);
  lcd.setCursor(13, 0); lcd.write((byte)2);
  lcd.setCursor(14, 0); lcd.write((byte)3);
  lcd.setCursor(15, 0); lcd.write((byte)4);
  lcd.setCursor(11, 1); lcd.write((byte)0);
  lcd.setCursor(12, 1); lcd.write((byte)6);
  lcd.setCursor(13, 1); lcd.write((byte)5);
  lcd.setCursor(2, 1);
  lcd.print("InVino");
}

void animacaoInicial() {
  lcd.createChar(0, desenhoC1_11);
  lcd.createChar(1, desenhoF0_12);
  lcd.createChar(2, desenhoC0_13);
  lcd.createChar(3, desenhoC1_14);
  lcd.createChar(4, desenhoC0_15);
  lcd.createChar(5, desenhoC1_13);

  lcd.clear();
  lcd.createChar(6, liquidoFrame1);
  desenharCenarioCompleto();
  delay(800);
  lcd.createChar(6, liquidoFrame2);
  desenharCenarioCompleto();
  delay(800);
  lcd.createChar(6, liquidoFrame3);
  desenharCenarioCompleto();
  delay(1200);
  lcd.clear();
}
// ----------------------------- Setup e loop -----------------------------

void setup() {
  Serial.begin(115200);
  pinMode(LED_AZUL, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(RGB_R, OUTPUT);
  pinMode(RGB_G, OUTPUT);
  pinMode(RGB_B, OUTPUT);
  definirRGB(false, false, false);
  digitalWrite(LED_AZUL, LOW);

  dht.begin();

  Wire.begin(21, 22);  // SDA, SCL
  lcd.init();
  lcd.backlight();
  animacaoInicial();
  lcd.createChar(0, simboloTemperatura);
  lcd.createChar(1, simboloUmidade);
  lcd.createChar(2, simboloLuminosidade);
  lcd.setCursor(0, 0);
  lcd.print("InVino");
  lcd.setCursor(0, 1);
  lcd.print("Conectando WiFi");

  conectarWiFi();
  mqtt.setServer(BROKER_MQTT, BROKER_PORT);
  mqtt.setCallback(callbackMQTT);

  enviarMedicoes();  // a primeira leitura já aparece no display
}

void loop() {
  // Reconecta sem travar o LED e o buzzer
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - ultimaReconexao >= INTERVALO_RECONEXAO) {
      ultimaReconexao = millis();
      WiFi.reconnect();
    }
  } else if (!mqtt.connected()) {
    if (millis() - ultimaReconexao >= INTERVALO_RECONEXAO) {
      ultimaReconexao = millis();
      conectarMQTT();
    }
  } else {
    mqtt.loop();
  }

  // Envia medições periodicamente
  if (millis() - ultimoEnvio >= INTERVALO_ENVIO) {
    ultimoEnvio = millis();
    enviarMedicoes();
  }

  // Teste pelo Monitor Serial (ex.: alertaTemp|on)
  if (Serial.available()) {
    String linha = Serial.readStringUntil('\n');
    processarComando(linha);
  }

  atualizarLed();
  atualizarRGB();
  atualizarSom();
}
