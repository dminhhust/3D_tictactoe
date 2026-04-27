from __future__ import annotations

from copy import deepcopy
import random
from typing import Any

N = 5
HALF = 2
FACE_NAMES = ["F", "B", "R", "L", "U", "D"]
PLAYER_MARKS = ["X", "O"]
ROTATION_AXES = ("x", "y", "z")


def rotate_vec_int(v: dict[str, int], axis: str, direction: int) -> dict[str, int]:
    d = 1 if direction > 0 else -1
    if axis == "x":
        return {"x": v["x"], "y": -v["z"], "z": v["y"]} if d > 0 else {"x": v["x"], "y": v["z"], "z": -v["y"]}
    if axis == "y":
        return {"x": v["z"], "y": v["y"], "z": -v["x"]} if d > 0 else {"x": -v["z"], "y": v["y"], "z": v["x"]}
    return {"x": -v["y"], "y": v["x"], "z": v["z"]} if d > 0 else {"x": v["y"], "y": -v["x"], "z": v["z"]}


def make_face_grid() -> list[list[Any]]:
    return [[None for _ in range(N)] for _ in range(N)]


def get_face_cell(sticker: dict[str, Any]) -> dict[str, Any]:
    x, y, z = sticker["cubie"]["x"], sticker["cubie"]["y"], sticker["cubie"]["z"]
    n = sticker["normal"]
    if n["z"] == 1:
        return {"face": "F", "row": HALF - y, "col": x + HALF}
    if n["z"] == -1:
        return {"face": "B", "row": HALF - y, "col": HALF - x}
    if n["x"] == 1:
        return {"face": "R", "row": HALF - y, "col": HALF - z}
    if n["x"] == -1:
        return {"face": "L", "row": HALF - y, "col": z + HALF}
    if n["y"] == 1:
        return {"face": "U", "row": z + HALF, "col": x + HALF}
    return {"face": "D", "row": HALF - z, "col": x + HALF}


def build_face_grids(state: dict[str, Any]) -> dict[str, Any]:
    grids = {face: make_face_grid() for face in FACE_NAMES}
    for sticker in state["stickers"]:
        cell = get_face_cell(sticker)
        grids[cell["face"]][cell["row"]][cell["col"]] = {"colorIndex": sticker["colorIndex"], "mark": sticker["mark"]}
    return grids


def check_four_consecutive(face_grid: list[list[Any]], mark: str) -> bool:
    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
    for r in range(N):
        for c in range(N):
            for dr, dc in directions:
                ok = True
                for k in range(4):
                    rr = r + dr * k
                    cc = c + dc * k
                    if rr < 0 or rr >= N or cc < 0 or cc >= N:
                        ok = False
                        break
                    cell = face_grid[rr][cc]
                    if not cell or cell["mark"] != mark:
                        ok = False
                        break
                if ok:
                    return True
    return False


def detect_mark_win(grids: dict[str, Any], mark: str) -> bool:
    return any(check_four_consecutive(grids[face], mark) for face in FACE_NAMES)


def detect_color_face_win(grids: dict[str, Any]) -> bool:
    for face in FACE_NAMES:
        grid = grids[face]
        base = grid[0][0]["colorIndex"] if grid[0][0] else None
        if base is None:
            continue
        all_same = True
        for r in range(N):
            for c in range(N):
                cell = grid[r][c]
                if not cell or cell["colorIndex"] != base:
                    all_same = False
                    break
            if not all_same:
                break
        if all_same:
            return True
    return False


def is_board_full(state: dict[str, Any]) -> bool:
    return all(s["mark"] is not None for s in state["stickers"])


def evaluate_end_state(state: dict[str, Any], active_player: int, action_label: str) -> dict[str, Any]:
    grids = build_face_grids(state)
    active_mark = PLAYER_MARKS[active_player]
    opponent = 1 - active_player
    opponent_mark = PLAYER_MARKS[opponent]
    active_line = detect_mark_win(grids, active_mark)
    opp_line = detect_mark_win(grids, opponent_mark)
    color_win = detect_color_face_win(grids)
    active_any = active_line or color_win

    if active_any and opp_line:
        return {
            "done": True,
            "winner": active_player,
            "winnerMark": active_mark,
            "reason": f"Both winning after {action_label}. Active player priority.",
            "draw": False,
        }
    if active_any:
        reason = "4-in-a-row + color-face win" if (active_line and color_win) else ("4-in-a-row" if active_line else "color-face win")
        return {"done": True, "winner": active_player, "winnerMark": active_mark, "reason": reason, "draw": False}
    if opp_line:
        return {"done": True, "winner": opponent, "winnerMark": opponent_mark, "reason": "Opponent line completed by active move", "draw": False}
    if is_board_full(state):
        return {"done": True, "winner": None, "winnerMark": None, "reason": "Draw", "draw": True}
    return {"done": False, "winner": None, "winnerMark": None, "reason": "", "draw": False}


def rotate_layer(state: dict[str, Any], axis: str, layer_coord: int, direction: int) -> None:
    for sticker in state["stickers"]:
        if sticker["cubie"][axis] != layer_coord:
            continue
        sticker["cubie"] = rotate_vec_int(sticker["cubie"], axis, direction)
        sticker["normal"] = rotate_vec_int(sticker["normal"], axis, direction)


def is_reverse_rotation_forbidden(state: dict[str, Any], acting_player: int, axis: str, layer_coord: int, direction: int) -> bool:
    last = state.get("last_rotation")
    if not last:
        return False
    if last.get("by_player") == acting_player:
        return False
    return last.get("axis") == axis and last.get("layer_coord") == layer_coord and int(last.get("dir", 0)) == -int(direction)


def apply_mark(state: dict[str, Any], player: int, sticker_id: int) -> dict[str, Any]:
    if state["game_over"]:
        return {"ok": False, "error": "Game is already finished"}
    if state["current_player"] != player:
        return {"ok": False, "error": "Not your turn"}
    if state["phase"] != "mark":
        return {"ok": False, "error": "Phase is not mark"}
    sticker = next((s for s in state["stickers"] if s["id"] == sticker_id), None)
    if not sticker or sticker["mark"] is not None:
        return {"ok": False, "error": "Sticker invalid or occupied"}

    sticker["mark"] = PLAYER_MARKS[player]
    result = evaluate_end_state(state, player, "mark")
    if result["done"]:
        _apply_terminal(state, result)
        return {"ok": True, "done": True, "result": result}
    state["phase"] = "rotate"
    return {"ok": True, "done": False, "result": result}


def apply_rotation(state: dict[str, Any], player: int, axis: str, layer_coord: int, direction: int) -> dict[str, Any]:
    if state["game_over"]:
        return {"ok": False, "error": "Game is already finished"}
    if state["current_player"] != player:
        return {"ok": False, "error": "Not your turn"}
    if state["phase"] != "rotate":
        return {"ok": False, "error": "Phase is not rotate"}
    if axis not in ROTATION_AXES or layer_coord not in {-2, -1, 0, 1, 2} or direction not in {-1, 1}:
        return {"ok": False, "error": "Invalid rotation"}
    if is_reverse_rotation_forbidden(state, player, axis, layer_coord, direction):
        return {"ok": False, "error": "Illegal rotation: you cannot reverse opponent's most recent rotation"}

    rotate_layer(state, axis, layer_coord, direction)
    state["last_rotation"] = {"by_player": player, "axis": axis, "layer_coord": layer_coord, "dir": direction}
    result = evaluate_end_state(state, player, "rotation")
    if result["done"]:
        _apply_terminal(state, result)
        return {"ok": True, "done": True, "result": result}
    state["current_player"] = 1 - state["current_player"]
    state["phase"] = "mark"
    return {"ok": True, "done": False, "result": result}


def apply_skip_rotation(state: dict[str, Any], player: int) -> dict[str, Any]:
    if state["game_over"]:
        return {"ok": False, "error": "Game is already finished"}
    if state["current_player"] != player:
        return {"ok": False, "error": "Not your turn"}
    if state["phase"] != "rotate":
        return {"ok": False, "error": "Phase is not rotate"}
    state["current_player"] = 1 - state["current_player"]
    state["phase"] = "mark"
    return {"ok": True, "done": False, "result": {"done": False}}


def serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_player": state["current_player"],
        "phase": state["phase"],
        "game_over": state["game_over"],
        "winner": state["winner"],
        "winner_mark": state["winner_mark"],
        "reason": state["reason"],
        "last_rotation": state.get("last_rotation"),
        "stickers": state["stickers"],
    }


def clone_state(state: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(state)


def create_initial_state(scramble_moves: int = 25) -> dict[str, Any]:
    state = new_solved_state()
    scramble_state(state, scramble_moves)
    return state


def new_solved_state() -> dict[str, Any]:
    stickers: list[dict[str, Any]] = []
    sid = 0
    for x in range(-HALF, HALF + 1):
        for y in range(-HALF, HALF + 1):
            for z in range(-HALF, HALF + 1):
                if x == HALF:
                    stickers.append(_new_sticker(sid, x, y, z, 1, 0, 0, "R")); sid += 1
                if x == -HALF:
                    stickers.append(_new_sticker(sid, x, y, z, -1, 0, 0, "L")); sid += 1
                if y == HALF:
                    stickers.append(_new_sticker(sid, x, y, z, 0, 1, 0, "U")); sid += 1
                if y == -HALF:
                    stickers.append(_new_sticker(sid, x, y, z, 0, -1, 0, "D")); sid += 1
                if z == HALF:
                    stickers.append(_new_sticker(sid, x, y, z, 0, 0, 1, "F")); sid += 1
                if z == -HALF:
                    stickers.append(_new_sticker(sid, x, y, z, 0, 0, -1, "B")); sid += 1
    return {
        "stickers": stickers,
        "current_player": 0,
        "phase": "mark",
        "game_over": False,
        "winner": None,
        "winner_mark": None,
        "reason": "",
        "last_rotation": None,
    }


def scramble_state(state: dict[str, Any], moves: int = 25) -> None:
    for _ in range(moves):
        axis = random.choice(ROTATION_AXES)
        layer = random.randint(-HALF, HALF)
        direction = random.choice([1, -1])
        rotate_layer(state, axis, layer, direction)
    state["last_rotation"] = None


def _apply_terminal(state: dict[str, Any], result: dict[str, Any]) -> None:
    state["game_over"] = True
    state["phase"] = "gameover"
    state["winner"] = result["winner"]
    state["winner_mark"] = result["winnerMark"]
    state["reason"] = result["reason"]


def _new_sticker(sid: int, x: int, y: int, z: int, nx: int, ny: int, nz: int, face: str) -> dict[str, Any]:
    return {
        "id": sid,
        "colorIndex": FACE_NAMES.index(face),
        "mark": None,
        "cubie": {"x": x, "y": y, "z": z},
        "normal": {"x": nx, "y": ny, "z": nz},
    }
