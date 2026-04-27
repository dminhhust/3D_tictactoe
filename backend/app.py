from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.game_logic import evaluate_end_state, is_reverse_rotation_forbidden, new_solved_state, rotate_layer, scramble_state

UTC = timezone.utc
ROOM_TTL = timedelta(minutes=30)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CreateRoomRequest(BaseModel):
    player_name: str = "Host"


@dataclass
class Room:
    room_id: str
    invite_token: str
    created_at: datetime
    last_active: datetime
    state: dict[str, Any]
    participants: dict[int, dict[str, Any]] = field(default_factory=dict)
    sockets: dict[int, WebSocket] = field(default_factory=dict)
    rematch_requester: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


app = FastAPI(title="3D TicTacToe Online Server")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT), name="static")
rooms: dict[str, Room] = {}


def now_utc() -> datetime:
    return datetime.now(UTC)


def prune_rooms() -> None:
    cutoff = now_utc() - ROOM_TTL
    stale = [rid for rid, room in rooms.items() if room.last_active < cutoff]
    for rid in stale:
        rooms.pop(rid, None)


def room_state_payload(room: Room) -> dict[str, Any]:
    return {
        "current_player": room.state["current_player"],
        "phase": room.state["phase"],
        "game_over": room.state["game_over"],
        "winner": room.state["winner"],
        "winner_mark": room.state["winner_mark"],
        "reason": room.state["reason"],
        "last_rotation": room.state.get("last_rotation"),
        "stickers": room.state["stickers"],
    }


async def broadcast(room: Room, payload: dict[str, Any]) -> None:
    drop: list[int] = []
    for slot, ws in room.sockets.items():
        try:
            await ws.send_json(payload)
        except Exception:
            drop.append(slot)
    for slot in drop:
        room.sockets.pop(slot, None)
        if slot in room.participants:
            room.participants[slot]["connected"] = False


def assign_slot(room: Room, player_name: str) -> int | None:
    # Reconnect by same name first.
    for slot, p in room.participants.items():
        if p["name"] == player_name and not p["connected"]:
            return slot
    for slot in (0, 1):
        if slot not in room.participants:
            return slot
    for slot, p in room.participants.items():
        if not p["connected"]:
            return slot
    return None


def validate_invite(room: Room, token: str) -> bool:
    return secrets.compare_digest(room.invite_token, token)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "index.html")


@app.post("/api/rooms")
async def create_room(req: CreateRoomRequest, websocket_base: str | None = None) -> dict[str, Any]:
    prune_rooms()
    room_id = uuid4().hex[:10]
    token = secrets.token_urlsafe(16)
    state = new_solved_state()
    scramble_state(state, 25)
    room = Room(
        room_id=room_id,
        invite_token=token,
        created_at=now_utc(),
        last_active=now_utc(),
        state=state,
    )
    rooms[room_id] = room
    room.participants[0] = {"name": req.player_name.strip() or "Host", "connected": False}
    invite_path = f"/invite/{room_id}/{token}"
    return {
        "room_id": room_id,
        "invite_token": token,
        "invite_url": invite_path,
        "host_slot": 0,
    }


@app.get("/invite/{room_id}/{invite_token}")
async def invite_redirect(room_id: str, invite_token: str) -> RedirectResponse:
    prune_rooms()
    room = rooms.get(room_id)
    if room is None or not validate_invite(room, invite_token):
        raise HTTPException(status_code=404, detail="Invitation invalid or expired")
    room.last_active = now_utc()
    return RedirectResponse(url=f"/?mode=online_duel&room_id={room_id}&token={invite_token}")


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str, token: str = Query(...)) -> dict[str, Any]:
    prune_rooms()
    room = rooms.get(room_id)
    if room is None or not validate_invite(room, token):
        raise HTTPException(status_code=404, detail="Room not found")
    room.last_active = now_utc()
    return {
        "room_id": room.room_id,
        "created_at": room.created_at.isoformat(),
        "last_active": room.last_active.isoformat(),
        "participants": [
            {"slot": slot, "name": p["name"], "connected": p["connected"]}
            for slot, p in sorted(room.participants.items())
        ],
        "game": room_state_payload(room),
    }


@app.websocket("/ws/rooms/{room_id}")
async def room_ws(
    websocket: WebSocket,
    room_id: str,
    token: str = Query(...),
    player_name: str = Query("Player"),
) -> None:
    prune_rooms()
    room = rooms.get(room_id)
    if room is None:
        await websocket.close(code=4404, reason="Room not found")
        return
    if not validate_invite(room, token):
        await websocket.close(code=4403, reason="Bad token")
        return

    await websocket.accept()
    async with room.lock:
        slot = assign_slot(room, (player_name or "Player").strip())
        if slot is None:
            await websocket.send_json({"type": "error", "message": "Room is full"})
            await websocket.close(code=4409, reason="Room full")
            return
        room.participants[slot] = {"name": (player_name or f"Player {slot+1}").strip(), "connected": True}
        room.sockets[slot] = websocket
        room.last_active = now_utc()
        await websocket.send_json(
            {
                "type": "joined",
                "room_id": room.room_id,
                "slot": slot,
                "player_mark": "X" if slot == 0 else "O",
                "participants": [
                    {"slot": s, "name": p["name"], "connected": p["connected"]}
                    for s, p in sorted(room.participants.items())
                ],
            }
        )
        await websocket.send_json({"type": "state_snapshot", "game": room_state_payload(room)})
        await broadcast(
            room,
            {
                "type": "turn_update",
                "current_player": room.state["current_player"],
                "phase": room.state["phase"],
                "participants": [
                    {"slot": s, "name": p["name"], "connected": p["connected"]}
                    for s, p in sorted(room.participants.items())
                ],
            },
        )

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            async with room.lock:
                room.last_active = now_utc()
                if mtype == "sync_request":
                    await websocket.send_json({"type": "state_snapshot", "game": room_state_payload(room)})
                    continue
                if mtype == "leave":
                    break
                if mtype == "join":
                    await websocket.send_json({"type": "joined", "room_id": room.room_id, "slot": slot})
                    continue
                if mtype == "request_new_game":
                    if room.rematch_requester is not None:
                        await websocket.send_json({"type": "error", "message": "A rematch request is already pending"})
                        continue
                    other_slot = 1 - slot
                    other_ws = room.sockets.get(other_slot)
                    if other_ws is None or other_slot not in room.participants or not room.participants[other_slot]["connected"]:
                        await websocket.send_json({"type": "error", "message": "Opponent is not connected"})
                        continue
                    room.rematch_requester = slot
                    await websocket.send_json({"type": "rematch_pending", "message": "Waiting for opponent response..."})
                    await other_ws.send_json(
                        {
                            "type": "rematch_request",
                            "from_slot": slot,
                            "from_name": room.participants.get(slot, {}).get("name", f"Player {slot+1}"),
                            "message": "Opponent requests a new game. Accept?",
                        }
                    )
                    continue
                if mtype == "new_game_response":
                    requester = room.rematch_requester
                    if requester is None:
                        await websocket.send_json({"type": "error", "message": "No pending rematch request"})
                        continue
                    responder = 1 - requester
                    if slot != responder:
                        await websocket.send_json({"type": "error", "message": "Only the opponent can answer this rematch request"})
                        continue
                    accepted = bool(msg.get("accepted"))
                    room.rematch_requester = None
                    if not accepted:
                        await broadcast(
                            room,
                            {
                                "type": "rematch_result",
                                "accepted": False,
                                "by_slot": slot,
                                "message": "Rematch declined. Continue current game.",
                            },
                        )
                        continue
                    room.state = new_solved_state()
                    scramble_state(room.state, 25)
                    await broadcast(
                        room,
                        {
                            "type": "rematch_result",
                            "accepted": True,
                            "by_slot": slot,
                            "message": "Rematch accepted. New game started.",
                        },
                    )
                    await broadcast(room, {"type": "state_snapshot", "game": room_state_payload(room)})
                    await broadcast(
                        room,
                        {"type": "turn_update", "current_player": room.state["current_player"], "phase": room.state["phase"]},
                    )
                    continue
                if room.state["game_over"]:
                    await websocket.send_json({"type": "error", "message": "Game is already finished"})
                    continue
                if slot != room.state["current_player"]:
                    await websocket.send_json({"type": "error", "message": "Not your turn"})
                    continue

                if mtype == "make_mark":
                    if room.state["phase"] != "mark":
                        await websocket.send_json({"type": "error", "message": "Phase is not mark"})
                        continue
                    sticker_id = msg.get("sticker_id")
                    sticker = next((s for s in room.state["stickers"] if s["id"] == sticker_id), None)
                    if not sticker or sticker["mark"] is not None:
                        await websocket.send_json({"type": "error", "message": "Sticker invalid or occupied"})
                        continue
                    sticker["mark"] = "X" if slot == 0 else "O"
                    result = evaluate_end_state(room.state, slot, "mark")
                    await broadcast(
                        room,
                        {"type": "move_applied", "action": {"type": "make_mark", "sticker_id": sticker_id}, "by": slot},
                    )
                    if result["done"]:
                        room.state["game_over"] = True
                        room.state["winner"] = result["winner"]
                        room.state["winner_mark"] = result["winnerMark"]
                        room.state["reason"] = result["reason"]
                        await broadcast(room, {"type": "state_snapshot", "game": room_state_payload(room)})
                        await broadcast(
                            room,
                            {
                                "type": "game_over",
                                "winner": room.state["winner"],
                                "winner_mark": room.state["winner_mark"],
                                "reason": room.state["reason"],
                                "draw": result["draw"],
                            },
                        )
                    else:
                        room.state["phase"] = "rotate"
                        await broadcast(room, {"type": "state_snapshot", "game": room_state_payload(room)})
                        await broadcast(
                            room,
                            {"type": "turn_update", "current_player": room.state["current_player"], "phase": room.state["phase"]},
                        )
                    continue

                if mtype == "rotate_layer":
                    if room.state["phase"] != "rotate":
                        await websocket.send_json({"type": "error", "message": "Phase is not rotate"})
                        continue
                    axis = msg.get("axis")
                    layer_coord = msg.get("layer_coord")
                    direction = msg.get("dir")
                    if axis not in {"x", "y", "z"} or layer_coord not in {-2, -1, 0, 1, 2} or direction not in {-1, 1}:
                        await websocket.send_json({"type": "error", "message": "Invalid rotation"})
                        continue
                    if is_reverse_rotation_forbidden(room.state, slot, axis, layer_coord, direction):
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": "Illegal rotation: you cannot reverse opponent's most recent rotation",
                            }
                        )
                        continue
                    rotate_layer(room.state, axis, layer_coord, direction)
                    room.state["last_rotation"] = {
                        "by_player": slot,
                        "axis": axis,
                        "layer_coord": layer_coord,
                        "dir": direction,
                    }
                    result = evaluate_end_state(room.state, slot, "rotation")
                    await broadcast(
                        room,
                        {
                            "type": "move_applied",
                            "action": {"type": "rotate_layer", "axis": axis, "layer_coord": layer_coord, "dir": direction},
                            "by": slot,
                        },
                    )
                    if result["done"]:
                        room.state["game_over"] = True
                        room.state["winner"] = result["winner"]
                        room.state["winner_mark"] = result["winnerMark"]
                        room.state["reason"] = result["reason"]
                        await broadcast(room, {"type": "state_snapshot", "game": room_state_payload(room)})
                        await broadcast(
                            room,
                            {
                                "type": "game_over",
                                "winner": room.state["winner"],
                                "winner_mark": room.state["winner_mark"],
                                "reason": room.state["reason"],
                                "draw": result["draw"],
                            },
                        )
                    else:
                        room.state["current_player"] = 1 - room.state["current_player"]
                        room.state["phase"] = "mark"
                        await broadcast(room, {"type": "state_snapshot", "game": room_state_payload(room)})
                        await broadcast(
                            room,
                            {"type": "turn_update", "current_player": room.state["current_player"], "phase": room.state["phase"]},
                        )
                    continue

                if mtype == "skip_rotation":
                    if room.state["phase"] != "rotate":
                        await websocket.send_json({"type": "error", "message": "Phase is not rotate"})
                        continue
                    room.state["current_player"] = 1 - room.state["current_player"]
                    room.state["phase"] = "mark"
                    await broadcast(room, {"type": "move_applied", "action": {"type": "skip_rotation"}, "by": slot})
                    await broadcast(room, {"type": "state_snapshot", "game": room_state_payload(room)})
                    await broadcast(
                        room,
                        {"type": "turn_update", "current_player": room.state["current_player"], "phase": room.state["phase"]},
                    )
                    continue

                await websocket.send_json({"type": "error", "message": "Unknown message type"})
    except WebSocketDisconnect:
        pass
    finally:
        async with room.lock:
            if room.sockets.get(slot) is websocket:
                room.sockets.pop(slot, None)
            if slot in room.participants:
                room.participants[slot]["connected"] = False
            if room.rematch_requester is not None:
                if slot == room.rematch_requester or slot == (1 - room.rematch_requester):
                    room.rematch_requester = None
                    await broadcast(
                        room,
                        {
                            "type": "rematch_result",
                            "accepted": False,
                            "by_slot": slot,
                            "message": "Rematch request canceled (player disconnected).",
                        },
                    )
            room.last_active = now_utc()
            await broadcast(room, {"type": "peer_left", "slot": slot})
