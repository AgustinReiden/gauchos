"""
GauchOS Voice Agent Server — FastAPI HTTP server for Kapso integration.

Exposes POST /start endpoint that Kapso calls to initiate a voice session.
Creates a Daily room, starts the Pipecat pipeline, and returns room credentials.
"""

import os
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

from gauchOS_voice_agent import create_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gauchOS-server")

# ── Environment ───────────────────────────────────────────────────────────

DAILY_API_KEY = os.environ["DAILY_API_KEY"]
DAILY_API_URL = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1")
KAPSO_API_KEY = os.environ.get("KAPSO_API_KEY", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "7860"))

# ── Active sessions tracking ──────────────────────────────────────────────

active_sessions: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"GauchOS Voice Agent Server starting on {HOST}:{PORT}")
    yield
    # Cleanup active sessions on shutdown
    for session_id, task in active_sessions.items():
        task.cancel()
    logger.info("Server shutting down, cancelled all active sessions")


app = FastAPI(
    title="GauchOS Voice Agent",
    description="Pipecat voice agent server for WhatsApp calls via Kapso",
    lifespan=lifespan,
)


# ── Daily.co room management ─────────────────────────────────────────────

async def create_daily_room(session_id: str) -> dict:
    """Create a Daily room and return room URL + meeting token."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Create room
        room_response = await client.post(
            f"{DAILY_API_URL}/rooms",
            headers={"Authorization": f"Bearer {DAILY_API_KEY}"},
            json={
                "name": f"gauchOS-{session_id[:8]}",
                "properties": {
                    "exp": int(asyncio.get_event_loop().time()) + 3600,  # 1 hour expiry
                    "enable_chat": False,
                    "enable_screenshare": False,
                    "max_participants": 2,
                },
            },
        )
        room_response.raise_for_status()
        room_data = room_response.json()
        room_url = room_data["url"]
        room_name = room_data["name"]

        # Create meeting token for the bot
        token_response = await client.post(
            f"{DAILY_API_URL}/meeting-tokens",
            headers={"Authorization": f"Bearer {DAILY_API_KEY}"},
            json={
                "properties": {
                    "room_name": room_name,
                    "is_owner": True,
                    "exp": int(asyncio.get_event_loop().time()) + 3600,
                },
            },
        )
        token_response.raise_for_status()
        token_data = token_response.json()

        return {
            "room_url": room_url,
            "room_name": room_name,
            "token": token_data["token"],
        }


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.post("/start")
async def start_session(request: Request):
    """
    Start a new voice agent session.
    Called by Kapso when a WhatsApp voice call comes in.

    Returns room credentials for Daily WebRTC connection.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    session_id = body.get("session_id") or str(uuid.uuid4())

    logger.info(f"Starting voice session: {session_id}")

    try:
        # Create Daily room
        room = await create_daily_room(session_id)

        # Start pipeline in background
        async def run_pipeline():
            try:
                await create_pipeline(
                    room_url=room["room_url"],
                    token=room["token"],
                    session_id=session_id,
                )
            except Exception as e:
                logger.error(f"Pipeline error for session {session_id}: {e}")
            finally:
                active_sessions.pop(session_id, None)
                logger.info(f"Session ended: {session_id}")

        task = asyncio.create_task(run_pipeline())
        active_sessions[session_id] = task

        logger.info(f"Session {session_id} started, room: {room['room_url']}")

        return JSONResponse({
            "status": "ok",
            "session_id": session_id,
            "room_url": room["room_url"],
            "token": room["token"],
        })

    except httpx.HTTPError as e:
        logger.error(f"Daily API error: {e}")
        raise HTTPException(status_code=502, detail="Failed to create Daily room")
    except Exception as e:
        logger.error(f"Start session error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "active_sessions": len(active_sessions),
        "sessions": list(active_sessions.keys()),
    }


@app.post("/stop/{session_id}")
async def stop_session(session_id: str):
    """Manually stop a voice session."""
    task = active_sessions.get(session_id)
    if not task:
        raise HTTPException(status_code=404, detail="Session not found")

    task.cancel()
    active_sessions.pop(session_id, None)
    return {"status": "stopped", "session_id": session_id}


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )
