# API Document (FastAPI + WebSocket)

Base app file: `D:\self_study\3D_tictactoe\backend\app.py`  
Authoritative rules: `D:\self_study\3D_tictactoe\backend\game_logic.py`

## HTTP API

### `POST /api/rooms`
Create online duel room.

Request:

```json
{
  "player_name": "Host"
}
```

Response:

```json
{
  "room_id": "a1b2c3d4e5",
  "invite_token": "token-value",
  "invite_url": "/invite/{room_id}/{invite_token}",
  "host_slot": 0
}
```

### `GET /invite/{room_id}/{invite_token}`
Validates invite and redirects to:

`/?mode=online_duel&room_id={room_id}&token={invite_token}`

### `GET /api/rooms/{room_id}?token=...`
Returns room metadata + authoritative game snapshot.

## WebSocket API

### `GET /ws/rooms/{room_id}?token=...&player_name=...`

Server emits on connect:
- `joined`
- `state_snapshot`
- `turn_update`

### Client -> Server messages

- `{"type":"join"}`
- `{"type":"sync_request"}`
- `{"type":"make_mark","sticker_id":123}`
- `{"type":"rotate_layer","axis":"x|y|z","layer_coord":-2..2,"dir":-1|1}`
- `{"type":"skip_rotation"}`
- `{"type":"request_new_game"}`
- `{"type":"new_game_response","accepted":true|false}`
- `{"type":"leave"}`

### Server -> Client messages

- `joined`
- `state_snapshot`
- `turn_update`
- `move_applied`
- `game_over`
- `rematch_pending`
- `rematch_request`
- `rematch_result`
- `peer_left`
- `error`

## `state_snapshot` game payload

- `current_player: 0|1`
- `phase: "mark" | "rotate" | "gameover"`
- `game_over: boolean`
- `winner: number | null`
- `winner_mark: "X" | "O" | null`
- `reason: string`
- `last_rotation: {by_player,axis,layer_coord,dir} | null`
- `stickers: Sticker[]`

Sticker:
- `id: number`
- `colorIndex: number`
- `mark: "X" | "O" | null`
- `cubie: {x,y,z}`
- `normal: {x,y,z}`

Rule note:
- A player cannot rotate a layer by the exact inverse of the opponent's most recent rotation (`same axis + same layer + opposite dir`).
