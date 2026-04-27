# API Document (Backend-Authoritative)

Base server: `D:\self_study\3D_tictactoe\backend\app.py`  
Game engine: `D:\self_study\3D_tictactoe\backend\game_logic.py`  
AI engine: `D:\self_study\3D_tictactoe\backend\ai_mcts.py`

## HTTP

### Rooms (online duel)
- `POST /api/rooms` -> create invite room
- `GET /invite/{room_id}/{invite_token}` -> redirect to game URL with query params
- `GET /api/rooms/{room_id}?token=...` -> room metadata + authoritative snapshot

### Sessions (local modes via backend)
- `POST /api/sessions`
  - body: `{"mode":"pvp_local"|"pve_local","player_name":"Player"}`
  - response: `{"session_id","mode","socket_url"}`
- `GET /api/sessions/{session_id}` -> session metadata + snapshot
- `DELETE /api/sessions/{session_id}` -> cleanup

## WebSocket

### Online duel
- `GET /ws/rooms/{room_id}?token=...&player_name=...`

### Local backend sessions
- `GET /ws/sessions/{session_id}?player_name=...`

## Shared WS command contract

### Client -> Server
- `join`
- `sync_request`
- `make_mark` with `sticker_id`
- `rotate_layer` with `axis`, `layer_coord`, `dir`
- `skip_rotation`
- `request_new_game`
- `new_game_response` with `accepted` (required for online duel rematch)
- `leave`

### Server -> Client
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

## Snapshot payload (`state_snapshot.game`)
- `current_player: 0|1`
- `phase: "mark"|"rotate"|"gameover"`
- `game_over: boolean`
- `winner: number|null`
- `winner_mark: "X"|"O"|null`
- `reason: string`
- `last_rotation: {by_player,axis,layer_coord,dir}|null`
- `stickers: Sticker[]`

Sticker:
- `id`
- `colorIndex`
- `mark`
- `cubie {x,y,z}`
- `normal {x,y,z}`

## Rules enforced server-side
- Mark/rotate/skip phase order
- Turn ownership
- Win/draw resolution (including active-player priority edge case)
- Reverse-rotation guard:
  - A player cannot perform the exact inverse of opponent’s most recent rotation
