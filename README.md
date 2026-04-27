# 3D Rubik's Cube Tic-Tac-Toe

This project now uses a **frontend-backend architecture**:

- Frontend: `index.html` (Three.js game client)
- Backend: `backend/app.py` (FastAPI + WebSocket authority server)
- Shared backend rules: `backend/game_logic.py`

## Modes

- `PvP Local`: 2 humans on one screen
- `PvE Local (MCTS)`: human (X) vs AI (O), pure MCTS (time budget ~1.5s)
- `Online Duel`: server-authoritative two-player room via invitation link
- Reverse-rotation guard: a player cannot directly reverse the opponent's most recent Rubik layer rotation.

## Install and run

From `D:\self_study\3D_tictactoe`:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open:

[http://localhost:8000](http://localhost:8000)

You can also use:

```powershell
.\start_server.ps1
```

## Online duel flow

1. In game, set mode to **Online Duel**.
2. Click **Create Invite Link**.
3. Share the generated invite URL.
4. Guest opens the link; backend redirects to game URL and auto-connects to room.
5. Server validates all mark/rotate/skip actions and broadcasts authoritative state.
6. In online mode, pressing **New Game** sends a rematch request; opponent must accept to start a new game.

Room behavior:
- Ephemeral in-memory room (no DB).
- Rooms expire after inactivity or server restart.

## Cloudflared quick tunnel (host from your laptop)

1. Start FastAPI server (port 8000 by default).
2. In a second terminal:

```powershell
.\start_tunnel.ps1
```

Or directly:

```powershell
cloudflared tunnel --url http://localhost:8000
```

3. Cloudflared prints a temporary public URL.
4. Create room in Online Duel mode and share the invite link generated from that public URL.

## Controls (all modes)

- Camera: drag mouse (OrbitControls)
- Mark: click empty sticker
- Rotate phase:
  - Click translucent rotate buttons
  - Keyboard `1..5`, `A/D` or `Left/Right`
  - `Enter` to skip rotation
