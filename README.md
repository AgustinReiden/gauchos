# GauchOS Voice Agent — WhatsApp Voice via Kapso + Pipecat

Agente de voz conversacional para gestión ganadera. Recibe llamadas de WhatsApp,
procesa con Deepgram (STT) + Gemini Flash (LLM) + Cartesia (TTS), y ejecuta
operaciones contra la base de datos de GauchOS via Edge Function.

## Arquitectura

```
WhatsApp Call → Kapso → Daily.co Room ← Pipecat Bot (este servidor)
                                              │
                                    Deepgram STT (español)
                                    Gemini Flash (LLM + tools)
                                    Cartesia TTS
                                              │
                                    POST /voice-agent-handler
                                              │
                                    Supabase Edge Function
```

## Setup

### 1. Variables de entorno

Copiar `.env.example` a `.env` y completar con las API keys.

### 2. Deploy con Docker

```bash
docker compose up -d
```

El servidor corre en el puerto 7860.

### 3. Configurar Kapso

```bash
export KAPSO_API_KEY="tu_key"
export WHATSAPP_CONFIG_ID="tu_config_id"
export VPS_BASE_URL="https://voice.tudominio.com"
bash setup_kapso.sh
```

### 4. Verificar

```bash
curl https://voice.tudominio.com/health
```

Llamar al número de WhatsApp y hablar con el agente.

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| POST | /start | Kapso llama para iniciar sesión de voz |
| GET | /health | Health check |
| POST | /stop/{session_id} | Detener una sesión manualmente |
