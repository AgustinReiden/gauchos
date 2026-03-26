"""
GauchOS Voice Agent — Pipecat pipeline for WhatsApp voice calls via Kapso.

Pipeline: Deepgram STT (español) → Gemini Flash (LLM + tools) → Cartesia TTS
Transport: Daily WebRTC (bridged by Kapso from WhatsApp)
Tool: consultar_gauchOS → POST to Supabase Edge Function
"""

import os
import json
import logging

import httpx
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame, LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.transports.daily.transport import DailyParams, DailyTransport

logger = logging.getLogger("gauchOS-voice-agent")

# ── Environment ───────────────────────────────────────────────────────────

GAUCHOSHQ_EDGE_URL = os.environ["GAUCHOSHQ_EDGE_URL"]
GAUCHOSHQ_EDGE_KEY = os.environ.get("GAUCHOSHQ_EDGE_KEY", "")
ESTABLECIMIENTO_ID = os.environ["ESTABLECIMIENTO_ID"]
DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CARTESIA_API_KEY = os.environ["CARTESIA_API_KEY"]

# ── System prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Sos el asistente de voz de GauchOS, un sistema de gestión ganadera y agrícola para establecimientos rurales argentinos.

PERSONALIDAD:
- Hablás en español rioplatense, como un encargado de campo profesional.
- Sos directo, conciso y claro. Es una llamada telefónica, no un chat.
- Usá frases cortas. Nada de listas largas ni formateo.
- Si la respuesta tiene muchos datos, resumí lo más importante y preguntá si quieren más detalle.

CAPACIDADES:
- Podés consultar stock de hacienda, lotes, movimientos, insumos, pasturas, rotaciones, precios de mercado, animales individuales, datos SENASA, labores de campo y lluvias.
- Podés registrar movimientos de ganado (ingresos, egresos, transferencias, mortandad, nacimiento), movimientos de insumos, labores de campo y lluvias.
- Podés deshacer el último movimiento.

REGLAS DE CONFIRMACIÓN (MUY IMPORTANTE):
- Para CUALQUIER operación que modifique datos (registrar movimiento, labor, lluvia, deshacer), SIEMPRE:
  1. Primero usá la tool consultar_gauchOS para procesar el pedido
  2. Si la respuesta indica requires_confirmation, repetile al usuario lo que entendiste y preguntá "¿Confirmo?"
  3. Solo cuando el usuario diga "sí", "dale", "confirmá", "metele", llamá la tool con action="confirm" y el pending_code
  4. Si dice "no", "cancelá", "pará", llamá con action="cancel"
  5. NUNCA confirmes sin que el usuario lo diga explícitamente

MANEJO DE RUIDO:
- Si la transcripción parece incoherente o incompleta (palabras sueltas sin sentido), decí: "No te escuché bien, ¿podés repetir?"
- Si hay ambigüedad en nombres de lotes o categorías, preguntá para clarificar antes de registrar.

VOCABULARIO:
- "vacas" = vacas de cría. "vaquillas" = vaquillonas. "novillos" = novillos.
- "el papi" o "al papi" = un lote. "el 2011" = otro lote. Son nombres de lotes.
- "caravana" = identificador individual del animal.
- "cab" o "cabezas" = cantidad de animales.
- "mm" o "milímetros" = precipitación.

FLUJO DE CONVERSACIÓN:
- Cuando atiendas la llamada, saludá brevemente: "Hola, soy GauchOS. ¿En qué te puedo ayudar?"
- Después de cada operación exitosa, preguntá: "¿Necesitás algo más?"
- Si el usuario se despide, respondé brevemente y cortá.
"""

# ── Tool definitions ──────────────────────────────────────────────────────

consultar_gauchOS_schema = FunctionSchema(
    name="consultar_gauchOS",
    description="Consulta o registra datos en el sistema GauchOS de gestión ganadera. "
    "Usá esta herramienta para cualquier operación: consultas de stock, movimientos, "
    "registrar ingresos/egresos, labores, lluvias, etc. Enviá el mensaje del usuario tal cual.",
    properties={
        "message": {
            "type": "string",
            "description": "El mensaje del usuario en lenguaje natural, tal como lo dijo.",
        },
        "action": {
            "type": "string",
            "enum": ["process", "confirm", "cancel"],
            "description": "Acción a realizar. 'process' para procesar un nuevo mensaje. "
            "'confirm' para confirmar una operación pendiente. "
            "'cancel' para cancelar una operación pendiente.",
        },
        "pending_code": {
            "type": "string",
            "description": "Código de la operación pendiente (solo para action=confirm o action=cancel).",
        },
    },
    required=["message", "action"],
)

tools = ToolsSchema(standard_tools=[consultar_gauchOS_schema])


# ── Tool handler ──────────────────────────────────────────────────────────

# Per-session state for tracking pending confirmations
_session_pending: dict[str, str] = {}  # session_id -> pending_code


async def handle_consultar_gauchOS(
    params: FunctionCallParams,
    session_id: str,
):
    """Call the GauchOS Edge Function and return the result."""
    message = params.arguments.get("message", "")
    action = params.arguments.get("action", "process")
    pending_code = params.arguments.get("pending_code") or _session_pending.get(session_id)

    payload = {
        "message": message,
        "session_id": session_id,
        "establecimiento_id": ESTABLECIMIENTO_ID,
    }

    if action in ("confirm", "cancel") and pending_code:
        payload["action"] = action
        payload["pending_code"] = pending_code
        # Clear pending state after confirm/cancel
        _session_pending.pop(session_id, None)
    else:
        payload["action"] = "process"

    headers = {"Content-Type": "application/json"}
    if GAUCHOSHQ_EDGE_KEY:
        headers["Authorization"] = f"Bearer {GAUCHOSHQ_EDGE_KEY}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(GAUCHOSHQ_EDGE_URL, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()

        # Track pending confirmations
        if result.get("type") == "pending_confirmation" and result.get("pending_code"):
            _session_pending[session_id] = result["pending_code"]

        await params.result_callback(result)

    except httpx.TimeoutException:
        await params.result_callback({
            "type": "error",
            "response": "La consulta tardó demasiado. Intentá de nuevo.",
        })
    except Exception as e:
        logger.error(f"Edge function call failed: {e}")
        await params.result_callback({
            "type": "error",
            "response": "Hubo un error al conectar con GauchOS. Intentá de nuevo.",
        })


# ── Pipeline builder ──────────────────────────────────────────────────────

async def create_pipeline(room_url: str, token: str, session_id: str) -> PipelineRunner:
    """Create and run the Pipecat pipeline for a voice call."""

    transport = DailyTransport(
        room_url,
        token,
        "GauchOS",
        DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    threshold=0.6,       # Higher threshold for noisy field environments
                    min_speech_duration_ms=250,
                    min_silence_duration_ms=600,
                )
            ),
        ),
    )

    stt = DeepgramSTTService(
        api_key=DEEPGRAM_API_KEY,
        settings=DeepgramSTTService.Settings(
            language="es",
            model="nova-2",
            smart_format=True,
            punctuate=True,
            keywords=[
                # Cattle categories
                "novillos:2", "vaquillonas:2", "terneros:2", "terneras:2",
                "toros:2", "novillitos:2", "vacas:2", "bueyes:2",
                # Ranch terms
                "caravana:2", "tropa:2", "hacienda:2", "lote:2",
                "cab:1", "cabezas:1",
                # Operations
                "ingreso:1", "egreso:1", "transferencia:1", "mortandad:1",
                # Place names (common lot names)
                "puesto:1", "papi:1",
                # Insumos
                "ivermectina:2", "glifosato:2",
                # Weather
                "milímetros:1", "lluvia:1",
            ],
        ),
    )

    llm = GoogleLLMService(
        api_key=GEMINI_API_KEY,
        model="gemini-2.0-flash",
        settings=GoogleLLMService.Settings(
            temperature=0.3,
            max_tokens=512,
        ),
    )

    tts = CartesiaTTSService(
        api_key=CARTESIA_API_KEY,
        settings=CartesiaTTSService.Settings(
            voice="15d0c2e2-8d29-44c3-be23-d585d5f154a1",  # "Pedro - Formal Speaker" (Spanish)
            language="es",
            speed=1.1,  # Slightly faster for concise field communication
            sample_rate=24000,
        ),
    )

    # Register tool handler with session context
    async def tool_handler(params: FunctionCallParams):
        await handle_consultar_gauchOS(params, session_id)

    llm.register_function(
        "consultar_gauchOS",
        tool_handler,
        cancel_on_interruption=True,
        timeout_secs=20.0,
    )

    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=tools,
    )

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    threshold=0.6,
                    min_speech_duration_ms=250,
                    min_silence_duration_ms=600,
                )
            ),
        ),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    # Event handlers
    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        transport.capture_participant_transcription(participant["id"])
        # Greet the user after a short pause
        await task.queue_frame(
            LLMMessagesAppendFrame(
                [{"role": "system", "content": "El usuario acaba de conectarse. Saludalo brevemente."}],
                run_llm=True,
            )
        )

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        # Clean up session state
        _session_pending.pop(session_id, None)
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    await runner.run(task)
    return runner
