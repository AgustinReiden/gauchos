"""
GauchOS Voice Agent Server — FastAPI HTTP server for voice calls.

Exposes POST /start-web endpoint for browser-based WebRTC voice calls.
Creates a Daily room, starts the Pipecat pipeline, and returns room credentials.
"""

import os
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from gauchOS_voice_agent import create_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gauchOS-server")

# ── Environment ───────────────────────────────────────────────────────────

DAILY_API_KEY = os.environ["DAILY_API_KEY"]
DAILY_API_URL = os.environ.get("DAILY_API_URL", "https://api.daily.co/v1")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "https://shycia.com.ar,http://localhost:5173").split(",")
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
    description="Pipecat voice agent server for browser WebRTC calls",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
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


async def create_user_token(room_name: str) -> str:
    """Create a non-owner meeting token for the browser user."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{DAILY_API_URL}/meeting-tokens",
            headers={"Authorization": f"Bearer {DAILY_API_KEY}"},
            json={
                "properties": {
                    "room_name": room_name,
                    "is_owner": False,
                    "exp": int(asyncio.get_event_loop().time()) + 3600,
                },
            },
        )
        response.raise_for_status()
        return response.json()["token"]


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.post("/start-web")
async def start_web_session(request: Request):
    """
    Start a voice session from the browser app.
    Creates a Daily room, starts the Pipecat bot, and returns
    a user token for the browser to join the same room.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    session_id = str(uuid.uuid4())
    establecimiento_id = body.get("establecimiento_id", "")

    logger.info(f"Starting web voice session: {session_id} (est: {establecimiento_id})")

    try:
        # Create Daily room + bot token
        room = await create_daily_room(session_id)

        # Create a separate user token for the browser
        user_token = await create_user_token(room["room_name"])

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

        logger.info(f"Web session {session_id} started, room: {room['room_url']}")

        return JSONResponse({
            "status": "ok",
            "session_id": session_id,
            "room_url": room["room_url"],
            "token": user_token,
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
