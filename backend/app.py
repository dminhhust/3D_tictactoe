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

from backend.ai_mcts import compute_best_move_mcts
from backend import game_logic as gl

UTC = timezone.utc
ROOM_TTL = timedelta(minutes=30)
SESSION_TTL = timedelta(minutes=60)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CreateRoomRequest(BaseModel):
    player_name: str = "Host"


class CreateSessionRequest(BaseModel):
    mode: str
    player_name: str = "Player"


@dataclass
class Channel:
    channel_id: str
    kind: str  # room | session
    mode: str  # online_duel | pvp_local | pve_local
    created_at: datetime
    last_active: datetime
    state: dict[str, Any]
    invite_token: str | None = None
    participants: dict[int, dict[str, Any]] = field(default_factory=dict)
    sockets: dict[int, WebSocket] = field(default_factory=dict)
    rematch_requester: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


app = FastAPI(title="3D TicTacToe Backend")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT), name="static")
rooms: dict[str, Channel] = {}
sessions: dict[str, Channel] = {}


def now_utc() -> datetime:
    return datetime.now(UTC)


def prune_channels() -> None:
    room_cutoff = now_utc() - ROOM_TTL
    session_cutoff = now_utc() - SESSION_TTL
    for rid in [k for k, r in rooms.items() if r.last_active < room_cutoff]:
        rooms.pop(rid, None)
    for sid in [k for k, s in sessions.items() if s.last_active < session_cutoff]:
        sessions.pop(sid, None)


def serialize_participants(ch: Channel) -> list[dict[str, Any]]:
    return [{"slot": s, "name": p["name"], "connected": p["connected"]} for s, p in sorted(ch.participants.items())]


async def broadcast(ch: Channel, payload: dict[str, Any]) -> None:
    drop: list[int] = []
    for slot, ws in ch.sockets.items():
        try:
            await ws.send_json(payload)
        except Exception:
            drop.append(slot)
    for slot in drop:
        ch.sockets.pop(slot, None)
        if slot in ch.participants:
            ch.participants[slot]["connected"] = False


def validate_invite(ch: Channel, token: str) -> bool:
    if not ch.invite_token:
        return False
    return secrets.compare_digest(ch.invite_token, token)


def room_payload(ch: Channel) -> dict[str, Any]:
    return {
        "id": ch.channel_id,
        "kind": ch.kind,
        "mode": ch.mode,
        "created_at": ch.created_at.isoformat(),
        "last_active": ch.last_active.isoformat(),
        "participants": serialize_participants(ch),
        "game": gl.serialize_state(ch.state),
    }


def assign_room_slot(ch: Channel, player_name: str) -> int | None:
    for slot, p in ch.participants.items():
        if p["name"] == player_name and not p["connected"]:
            return slot
    for slot in (0, 1):
        if slot not in ch.participants:
            return slot
    for slot, p in ch.participants.items():
        if not p["connected"]:
            return slot
    return None


def local_actor_for_action(ch: Channel, controller_slot: int) -> int:
    # pvp_local and pve_local actions are driven by single controller socket.
    # pvp_local: controller can act for current turn owner
    # pve_local: human is slot 0 only; AI handled server-side
    if ch.mode == "pvp_local":
        return ch.state["current_player"]
    if ch.mode == "pve_local":
        return 0
    return controller_slot


async def emit_state(ch: Channel) -> None:
    await broadcast(ch, {"type": "state_snapshot", "game": gl.serialize_state(ch.state)})
    await broadcast(ch, {"type": "turn_update", "current_player": ch.state["current_player"], "phase": ch.state["phase"]})
    if ch.state["game_over"]:
        await broadcast(
            ch,
            {
                "type": "game_over",
                "winner": ch.state["winner"],
                "winner_mark": ch.state["winner_mark"],
                "reason": ch.state["reason"],
                "draw": ch.state["winner"] is None,
            },
        )


async def run_pve_ai_if_needed(ch: Channel) -> None:
    # Only for local PvE session and only when it's AI turn at mark phase.
    if ch.mode != "pve_local":
        return
    while not ch.state["game_over"] and ch.state["current_player"] == 1 and ch.state["phase"] == "mark":
        mv = compute_best_move_mcts(gl.clone_state(ch.state), 1500)
        if not mv:
            break

        mark = gl.apply_mark(ch.state, 1, int(mv["sticker_id"]))
        if not mark["ok"]:
            await broadcast(ch, {"type": "error", "message": "AI failed to make mark"})
            break
        await broadcast(ch, {"type": "move_applied", "action": {"type": "make_mark", "sticker_id": int(mv["sticker_id"])}, "by": "ai"})
        if mark["done"]:
            await emit_state(ch)
            break

        rot = mv.get("rotation")
        if rot is None:
            skip = gl.apply_skip_rotation(ch.state, 1)
            if not skip["ok"]:
                await broadcast(ch, {"type": "error", "message": "AI failed to skip rotation"})
                break
            await broadcast(ch, {"type": "move_applied", "action": {"type": "skip_rotation"}, "by": "ai"})
        else:
            rot_res = gl.apply_rotation(ch.state, 1, rot["axis"], int(rot["layer_coord"]), int(rot["dir"]))
            if not rot_res["ok"]:
                # fallback to skip to avoid deadlock
                skip = gl.apply_skip_rotation(ch.state, 1)
                if skip["ok"]:
                    await broadcast(ch, {"type": "move_applied", "action": {"type": "skip_rotation"}, "by": "ai"})
                else:
                    await broadcast(ch, {"type": "error", "message": "AI failed to complete turn"})
                    break
            else:
                await broadcast(
                    ch,
                    {
                        "type": "move_applied",
                        "action": {"type": "rotate_layer", "axis": rot["axis"], "layer_coord": int(rot["layer_coord"]), "dir": int(rot["dir"])},
                        "by": "ai",
                    },
                )
        await emit_state(ch)


async def process_game_action(ch: Channel, mtype: str, msg: dict[str, Any], actor_slot: int, allow_controller: bool) -> tuple[bool, str | None]:
    actor = local_actor_for_action(ch, actor_slot) if allow_controller else actor_slot
    if mtype == "make_mark":
        res = gl.apply_mark(ch.state, actor, int(msg.get("sticker_id")))
        if not res["ok"]:
            return False, res["error"]
        await broadcast(ch, {"type": "move_applied", "action": {"type": "make_mark", "sticker_id": int(msg.get("sticker_id"))}, "by": actor})
        await emit_state(ch)
        await run_pve_ai_if_needed(ch)
        return True, None

    if mtype == "rotate_layer":
        res = gl.apply_rotation(ch.state, actor, msg.get("axis"), int(msg.get("layer_coord")), int(msg.get("dir")))
        if not res["ok"]:
            return False, res["error"]
        await broadcast(
            ch,
            {
                "type": "move_applied",
                "action": {"type": "rotate_layer", "axis": msg.get("axis"), "layer_coord": int(msg.get("layer_coord")), "dir": int(msg.get("dir"))},
                "by": actor,
            },
        )
        await emit_state(ch)
        await run_pve_ai_if_needed(ch)
        return True, None

    if mtype == "skip_rotation":
        res = gl.apply_skip_rotation(ch.state, actor)
        if not res["ok"]:
            return False, res["error"]
        await broadcast(ch, {"type": "move_applied", "action": {"type": "skip_rotation"}, "by": actor})
        await emit_state(ch)
        await run_pve_ai_if_needed(ch)
        return True, None

    return False, "Unknown message type"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "index.html")


@app.post("/api/rooms")
async def create_room(req: CreateRoomRequest) -> dict[str, Any]:
    prune_channels()
    rid = uuid4().hex[:10]
    token = secrets.token_urlsafe(16)
    ch = Channel(
        channel_id=rid,
        kind="room",
        mode="online_duel",
        created_at=now_utc(),
        last_active=now_utc(),
        invite_token=token,
        state=gl.create_initial_state(25),
    )
    ch.participants[0] = {"name": req.player_name.strip() or "Host", "connected": False}
    rooms[rid] = ch
    return {"room_id": rid, "invite_token": token, "invite_url": f"/invite/{rid}/{token}", "host_slot": 0}


@app.get("/invite/{room_id}/{invite_token}")
async def invite_redirect(room_id: str, invite_token: str) -> RedirectResponse:
    prune_channels()
    ch = rooms.get(room_id)
    if ch is None or not validate_invite(ch, invite_token):
        raise HTTPException(status_code=404, detail="Invitation invalid or expired")
    ch.last_active = now_utc()
    return RedirectResponse(url=f"/?mode=online_duel&room_id={room_id}&token={invite_token}")


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str, token: str = Query(...)) -> dict[str, Any]:
    prune_channels()
    ch = rooms.get(room_id)
    if ch is None or not validate_invite(ch, token):
        raise HTTPException(status_code=404, detail="Room not found")
    ch.last_active = now_utc()
    return room_payload(ch)


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    prune_channels()
    mode = req.mode if req.mode in {"pvp_local", "pve_local"} else "pvp_local"
    sid = uuid4().hex[:12]
    ch = Channel(
        channel_id=sid,
        kind="session",
        mode=mode,
        created_at=now_utc(),
        last_active=now_utc(),
        state=gl.create_initial_state(25),
    )
    ch.participants[0] = {"name": req.player_name.strip() or "Player", "connected": False}
    sessions[sid] = ch
    return {"session_id": sid, "mode": mode, "socket_url": f"/ws/sessions/{sid}"}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    prune_channels()
    ch = sessions.get(session_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Session not found")
    ch.last_active = now_utc()
    return room_payload(ch)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    existed = sessions.pop(session_id, None) is not None
    return {"deleted": existed}


@app.websocket("/ws/rooms/{room_id}")
async def ws_room(websocket: WebSocket, room_id: str, token: str = Query(...), player_name: str = Query("Player")) -> None:
    prune_channels()
    ch = rooms.get(room_id)
    if ch is None:
        await websocket.close(code=4404, reason="Room not found")
        return
    if not validate_invite(ch, token):
        await websocket.close(code=4403, reason="Bad token")
        return
    await websocket.accept()

    async with ch.lock:
        slot = assign_room_slot(ch, (player_name or "Player").strip())
        if slot is None:
            await websocket.send_json({"type": "error", "message": "Room is full"})
            await websocket.close(code=4409, reason="Room full")
            return
        ch.participants[slot] = {"name": (player_name or f"Player {slot+1}").strip(), "connected": True}
        ch.sockets[slot] = websocket
        ch.last_active = now_utc()
        await websocket.send_json({"type": "joined", "room_id": ch.channel_id, "slot": slot, "player_mark": gl.PLAYER_MARKS[slot], "participants": serialize_participants(ch)})
        await websocket.send_json({"type": "state_snapshot", "game": gl.serialize_state(ch.state)})
        await broadcast(ch, {"type": "turn_update", "current_player": ch.state["current_player"], "phase": ch.state["phase"], "participants": serialize_participants(ch)})

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            async with ch.lock:
                ch.last_active = now_utc()
                if mtype == "sync_request":
                    await websocket.send_json({"type": "state_snapshot", "game": gl.serialize_state(ch.state)})
                    continue
                if mtype == "leave":
                    break
                if mtype == "join":
                    await websocket.send_json({"type": "joined", "room_id": ch.channel_id, "slot": slot})
                    continue
                if mtype == "request_new_game":
                    if ch.rematch_requester is not None:
                        await websocket.send_json({"type": "error", "message": "A rematch request is already pending"})
                        continue
                    other = 1 - slot
                    if other not in ch.sockets:
                        await websocket.send_json({"type": "error", "message": "Opponent is not connected"})
                        continue
                    ch.rematch_requester = slot
                    await websocket.send_json({"type": "rematch_pending", "message": "Waiting for opponent response..."})
                    await ch.sockets[other].send_json({"type": "rematch_request", "from_slot": slot, "from_name": ch.participants.get(slot, {}).get("name", f"Player {slot+1}")})
                    continue
                if mtype == "new_game_response":
                    requester = ch.rematch_requester
                    if requester is None:
                        await websocket.send_json({"type": "error", "message": "No pending rematch request"})
                        continue
                    responder = 1 - requester
                    if slot != responder:
                        await websocket.send_json({"type": "error", "message": "Only opponent can answer rematch request"})
                        continue
                    accepted = bool(msg.get("accepted"))
                    ch.rematch_requester = None
                    if not accepted:
                        await broadcast(ch, {"type": "rematch_result", "accepted": False, "message": "Rematch declined. Continue current game."})
                        continue
                    ch.state = gl.create_initial_state(25)
                    await broadcast(ch, {"type": "rematch_result", "accepted": True, "message": "Rematch accepted. New game started."})
                    await emit_state(ch)
                    continue
                ok, err = await process_game_action(ch, mtype, msg, slot, allow_controller=False)
                if not ok:
                    await websocket.send_json({"type": "error", "message": err})
    except WebSocketDisconnect:
        pass
    finally:
        async with ch.lock:
            if ch.sockets.get(slot) is websocket:
                ch.sockets.pop(slot, None)
            if slot in ch.participants:
                ch.participants[slot]["connected"] = False
            if ch.rematch_requester is not None:
                if slot == ch.rematch_requester or slot == (1 - ch.rematch_requester):
                    ch.rematch_requester = None
                    await broadcast(ch, {"type": "rematch_result", "accepted": False, "message": "Rematch request canceled (player disconnected)."})
            ch.last_active = now_utc()
            await broadcast(ch, {"type": "peer_left", "slot": slot})


@app.websocket("/ws/sessions/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str, player_name: str = Query("Player")) -> None:
    prune_channels()
    ch = sessions.get(session_id)
    if ch is None:
        await websocket.close(code=4404, reason="Session not found")
        return
    await websocket.accept()
    slot = 0

    async with ch.lock:
        ch.participants[slot] = {"name": (player_name or "Player").strip(), "connected": True}
        ch.sockets[slot] = websocket
        ch.last_active = now_utc()
        await websocket.send_json({"type": "joined", "session_id": ch.channel_id, "slot": slot, "controller": True, "mode": ch.mode})
        await websocket.send_json({"type": "state_snapshot", "game": gl.serialize_state(ch.state)})
        await broadcast(ch, {"type": "turn_update", "current_player": ch.state["current_player"], "phase": ch.state["phase"]})
        await run_pve_ai_if_needed(ch)

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            async with ch.lock:
                ch.last_active = now_utc()
                if mtype == "sync_request":
                    await websocket.send_json({"type": "state_snapshot", "game": gl.serialize_state(ch.state)})
                    continue
                if mtype == "leave":
                    break
                if mtype == "join":
                    await websocket.send_json({"type": "joined", "session_id": ch.channel_id, "slot": slot, "controller": True, "mode": ch.mode})
                    continue
                if mtype in {"request_new_game", "new_game_response"}:
                    ch.state = gl.create_initial_state(25)
                    await broadcast(ch, {"type": "rematch_result", "accepted": True, "message": "New local game started."})
                    await emit_state(ch)
                    await run_pve_ai_if_needed(ch)
                    continue
                ok, err = await process_game_action(ch, mtype, msg, slot, allow_controller=True)
                if not ok:
                    await websocket.send_json({"type": "error", "message": err})
    except WebSocketDisconnect:
        pass
    finally:
        async with ch.lock:
            if ch.sockets.get(slot) is websocket:
                ch.sockets.pop(slot, None)
            if slot in ch.participants:
                ch.participants[slot]["connected"] = False
            ch.last_active = now_utc()
