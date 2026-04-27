from __future__ import annotations

import math
import random
import time
from typing import Any

from backend import game_logic as gl


def compute_best_move_mcts(root_state: dict[str, Any], time_budget_ms: int = 1500) -> dict[str, Any] | None:
    ai_player = root_state["current_player"]

    def hash_state(state: dict[str, Any]) -> str:
        bits = [str(state["current_player"]), state["phase"]]
        last = state.get("last_rotation")
        bits.append("_" if not last else f'{last["by_player"]}{last["axis"]}{last["layer_coord"]}{last["dir"]}')
        for s in state["stickers"]:
            m = "_" if s["mark"] is None else s["mark"]
            bits.append(f'{s["id"]}:{m}:{s["cubie"]["x"]}{s["cubie"]["y"]}{s["cubie"]["z"]}:{s["normal"]["x"]}{s["normal"]["y"]}{s["normal"]["z"]}')
        return "|".join(bits)

    def legal_rotations(state: dict[str, Any], player: int) -> list[dict[str, Any] | None]:
        rots: list[dict[str, Any] | None] = [None]
        for axis in gl.ROTATION_AXES:
            for layer in (-2, -1, 0, 1, 2):
                for direction in (-1, 1):
                    if gl.is_reverse_rotation_forbidden(state, player, axis, layer, direction):
                        continue
                    rots.append({"axis": axis, "layer_coord": layer, "dir": direction})
        return rots

    def weighted_mark_sample(state: dict[str, Any], count: int) -> list[int]:
        empties = [s for s in state["stickers"] if s["mark"] is None]
        if not empties:
            return []
        if len(empties) <= count:
            return [s["id"] for s in empties]
        weighted = []
        for s in empties:
            d = abs(s["cubie"]["x"]) + abs(s["cubie"]["y"]) + abs(s["cubie"]["z"])
            w = 1.0 / (1 + d)
            weighted.append((s["id"], w))
        result: list[int] = []
        pool = weighted[:]
        for _ in range(count):
            total = sum(w for _, w in pool)
            r = random.random() * total
            pick_idx = 0
            for i, (_, w) in enumerate(pool):
                r -= w
                if r <= 0:
                    pick_idx = i
                    break
            sid, _ = pool.pop(pick_idx)
            result.append(sid)
        return result

    def legal_moves(state: dict[str, Any], mark_sample: int = 14, rotation_sample: int = 10) -> list[dict[str, Any]]:
        if state["game_over"] or state["phase"] != "mark":
            return []
        player = state["current_player"]
        mark_ids = weighted_mark_sample(state, mark_sample)
        rots = legal_rotations(state, player)
        if len(rots) > rotation_sample:
            non_skip = [r for r in rots if r is not None]
            random.shuffle(non_skip)
            rots = [None] + non_skip[: max(1, rotation_sample - 1)]
        moves = []
        for sid in mark_ids:
            for rot in rots:
                moves.append({"sticker_id": sid, "rotation": rot})
        random.shuffle(moves)
        return moves

    def apply_composite(state: dict[str, Any], move: dict[str, Any], player: int) -> dict[str, Any]:
        first = gl.apply_mark(state, player, move["sticker_id"])
        if not first["ok"]:
            return {"done": True, "winner": 1 - player, "draw": False, "illegal": True}
        if first["done"]:
            return first["result"]
        rot = move["rotation"]
        if rot is None:
            second = gl.apply_skip_rotation(state, player)
        else:
            second = gl.apply_rotation(state, player, rot["axis"], rot["layer_coord"], rot["dir"])
        if not second["ok"]:
            return {"done": True, "winner": 1 - player, "draw": False, "illegal": True}
        return second.get("result", {"done": False})

    def utility(result: dict[str, Any]) -> float:
        if result.get("draw"):
            return 0.0
        w = result.get("winner")
        if w == ai_player:
            return 1.0
        if w is None:
            return 0.0
        return -1.0

    class Node:
        def __init__(self, state: dict[str, Any], parent: Node | None = None, move: dict[str, Any] | None = None):
            self.state = state
            self.parent = parent
            self.move = move
            self.children: list[Node] = []
            self.visits = 0
            self.value = 0.0
            self.terminal: dict[str, Any] | None = None
            self.actions: list[dict[str, Any]] | None = None
            self.expanded = 0
            self.hash = hash_state(state)

        def can_expand(self) -> bool:
            if self.terminal is not None:
                return False
            if self.actions is None:
                self.actions = legal_moves(self.state)
            if not self.actions:
                return False
            max_children = max(1, int(1.9 * ((self.visits + 1) ** 0.55)))
            return self.expanded < len(self.actions) and len(self.children) < max_children

    def best_uct(node: Node) -> Node:
        best = node.children[0]
        best_score = -1e18
        c = 1.1 + 0.7 / math.sqrt(node.visits + 1)
        for ch in node.children:
            exploit = ch.value / (ch.visits or 1)
            explore = c * math.sqrt(math.log(node.visits + 1) / (ch.visits or 1))
            score = exploit + explore
            if score > best_score:
                best_score = score
                best = ch
        return best

    def rollout(state: dict[str, Any]) -> float:
        sim = gl.clone_state(state)
        for _ in range(20):
            if sim["game_over"] or sim["phase"] != "mark":
                break
            player = sim["current_player"]
            cand = legal_moves(sim, mark_sample=8, rotation_sample=6)
            if not cand:
                return 0.0
            mv = random.choice(cand)
            result = apply_composite(sim, mv, player)
            if result.get("done"):
                return utility(result)
        if sim["game_over"]:
            return utility({"winner": sim["winner"], "draw": sim["winner"] is None})
        return 0.0

    root = Node(gl.clone_state(root_state))
    if not root.can_expand() and not root.children:
        return None

    transpo: dict[str, tuple[int, float]] = {}
    deadline = time.perf_counter() + (time_budget_ms / 1000.0)
    while time.perf_counter() < deadline:
        node = root
        while not node.can_expand() and node.children:
            node = best_uct(node)

        if node.can_expand():
            mv = node.actions[node.expanded]  # type: ignore[index]
            node.expanded += 1
            nxt = gl.clone_state(node.state)
            result = apply_composite(nxt, mv, nxt["current_player"])
            child = Node(nxt, node, mv)
            if result.get("done"):
                child.terminal = result
            node.children.append(child)
            node = child

        val = utility(node.terminal) if node.terminal is not None else rollout(node.state)
        while node is not None:
            node.visits += 1
            node.value += val
            v, s = transpo.get(node.hash, (0, 0.0))
            transpo[node.hash] = (v + 1, s + val)
            node = node.parent

    if not root.children:
        return None
    best = max(root.children, key=lambda c: (c.visits, c.value / (c.visits or 1)))
    return best.move
