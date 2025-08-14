import asyncio
import json
import math
import os
import random
import re
import time
from typing import Dict, List
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


# Lifespan handler
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    # Startup
    spawn_food()
    game_task = asyncio.create_task(game_loop())
    try:
        yield
    finally:
        # Shutdown
        game_task.cancel()
        try:
            await game_task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=app_lifespan)

# Base & static path handling
BASE_DIR = Path(__file__).resolve().parent.parent  # project root
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Game constants
MAP_WIDTH = 3000
MAP_HEIGHT = 3000
TICK_RATE = 1/30  # 30 FPS server updates
FOOD_COUNT = 300
FOOD_VALUE = 3
INITIAL_RADIUS = 15
MOVE_SPEED = 2000  # base units / second (tăng từ 180)
SPEED_DECAY_FACTOR = 0.35  # speed scales by (mass ^ -factor)
COLLISION_EAT_RATIO = 1.0  # larger radius > smaller radius eats immediately
RESPAWN_DELAY = 2.0  # seconds delay before respawn
RESPAWN_RADIUS = 15

# Food and power-up settings
FOOD_MIN_R = 4
FOOD_MAX_R = 10
SPEED_FOOD_CHANCE = 0.30  # green
SHIELD_FOOD_CHANCE = 0.02  # red (giảm từ 5%)
BLUE_FOOD_CHANCE = 0.10  # blue
SPEED_BOOST = 1.20
SPEED_DURATION = 5.0
SHIELD_DURATION = 5.0
ATTRACTION_DURATION = 10.0
ATTRACTION_RANGE_PER_UNIT = 6.0  # nhỏ hơn

players: Dict[str, dict] = {}
foods: Dict[int, dict] = {}
connections: Dict[str, WebSocket] = {}

next_food_id = 0

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse(
            "<h1>Thiếu file index.html trong thư mục static</h1>"
        )
    with open(index_file, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


def random_position(radius=0):
    return {
        "x": random.uniform(radius, MAP_WIDTH - radius),
        "y": random.uniform(radius, MAP_HEIGHT - radius)
    }


def spawn_food():
    global next_food_id
    while len(foods) < FOOD_COUNT:
        fid = next_food_id
        next_food_id += 1
        pos = random_position()
        # Decide type by probability
        roll = random.random()
        if roll < SHIELD_FOOD_CHANCE:
            ftype = "shield"  # red
            color = "#ef4444"
        elif roll < SHIELD_FOOD_CHANCE + SPEED_FOOD_CHANCE:
            ftype = "speed"  # green
            color = "#22c55e"
        elif roll < SHIELD_FOOD_CHANCE + SPEED_FOOD_CHANCE + BLUE_FOOD_CHANCE:
            ftype = "attract"  # blue
            color = "#3b82f6"
        else:
            ftype = "normal"
            color = "#ffd54f"
        # Bán kính hạt: viên xanh dương nhỏ hơn
        if ftype == "attract":
            r = random.uniform(FOOD_MIN_R, FOOD_MIN_R + 2)
        else:
            r = random.uniform(FOOD_MIN_R, FOOD_MAX_R)
        # Value scales with area roughly based on baseline r=6
        base_area = math.pi * 6 * 6
        val = FOOD_VALUE * (math.pi * r * r) / base_area
        foods[fid] = {
            "id": fid,
            "x": pos["x"],
            "y": pos["y"],
            "r": r,
            "value": val,
            "type": ftype,
            "color": color,
        }


def mass_from_radius(r):
    return math.pi * r * r


def radius_from_mass(m):
    return math.sqrt(m / math.pi)


async def game_loop():
    # Main server tick loop
    last = time.perf_counter()
    while True:
        now = time.perf_counter()
        dt = now - last
        last = now

        # Update player positions
        for pid, p in list(players.items()):
            # Reset speed stack if expired
            if p.get("speedUntil", 0) <= now and p.get("speedStacks", 0) > 0:
                p["speedStacks"] = 0
            # Reset attraction range if expired
            if (
                p.get("attractUntil", 0) <= now
                and p.get("attractRange", 0) > 0
            ):
                p["attractRange"] = 0.0
            if not p.get("target"):
                continue
            tx, ty = p["target"]
            dx = tx - p["x"]
            dy = ty - p["y"]
            dist = math.hypot(dx, dy)
            if dist > 1e-3:
                # Speed slower with size + temporary speed boost
                mass = mass_from_radius(p["r"])
                # speed multiplier = 1 + 1% per stack while active
                if p.get("speedUntil", 0) > now:
                    speed_mul = 1.0 + 0.01 * float(
                        p.get("speedStacks", 0) or 0
                    )
                else:
                    speed_mul = 1.0
                speed = MOVE_SPEED * (mass ** -SPEED_DECAY_FACTOR) * speed_mul
                step = speed * dt
                if step >= dist:
                    p["x"], p["y"] = tx, ty
                else:
                    p["x"] += dx / dist * step
                    p["y"] += dy / dist * step
                # Clamp inside map
                p["x"] = max(p["r"], min(MAP_WIDTH - p["r"], p["x"]))
                p["y"] = max(p["r"], min(MAP_HEIGHT - p["r"], p["y"]))

        # Handle food collisions
        eaten_food_ids: List[int] = []
        food_items = list(foods.items())
        for pid, p in players.items():
            for fid, f in food_items:
                if fid in foods:
                    fr = float(f.get("r", 6))
                    extra = (
                        p.get("attractRange", 0.0)
                        if p.get("attractUntil", 0) > now
                        else 0.0
                    )
                    eff_r = p["r"] + fr + extra
                    if (
                        (p["x"] - f["x"]) ** 2 + (p["y"] - f["y"]) ** 2
                        <= eff_r ** 2
                    ):
                        # Apply power-up effects
                        ftype = f.get("type", "normal")
                        if ftype == "speed":
                            # stack + refresh (theo diện tích hạt)
                            fr = float(f.get("r", 6))
                            area_ratio = (fr / 6.0) ** 2
                            p["speedStacks"] = float(
                                p.get("speedStacks", 0) or 0
                            ) + area_ratio
                            p["speedUntil"] = now + SPEED_DURATION
                            # notify player: chỉ cập nhật góc phải
                            ws = connections.get(pid)
                            if ws:
                                try:
                                    await ws.send_text(
                                        json.dumps({
                                            "type": "effect",
                                            "effect": "speed",
                                            "stacks": p["speedStacks"],
                                            "duration": SPEED_DURATION,
                                        })
                                    )
                                except Exception:
                                    pass
                        elif ftype == "shield":
                            fr = float(f.get("r", 6))
                            area_ratio = (fr / 6.0) ** 2
                            duration = SHIELD_DURATION * area_ratio
                            p["invUntil"] = now + duration
                            # notify player: chỉ cập nhật góc phải
                            ws = connections.get(pid)
                            if ws:
                                try:
                                    await ws.send_text(
                                        json.dumps({
                                            "type": "effect",
                                            "effect": "shield",
                                            "duration": duration,
                                        })
                                    )
                                except Exception:
                                    pass
                        elif ftype == "attract":
                            fr = float(f.get("r", 6))
                            area_ratio = (fr / 6.0) ** 2
                            p["attractRange"] = float(
                                p.get("attractRange", 0.0) or 0.0
                            ) + ATTRACTION_RANGE_PER_UNIT * area_ratio
                            p["attractUntil"] = now + ATTRACTION_DURATION
                            # notify player
                            ws = connections.get(pid)
                            if ws:
                                try:
                                    await ws.send_text(
                                        json.dumps({
                                            "type": "effect",
                                            "effect": "attract",
                                            "range": p["attractRange"],
                                            "duration": ATTRACTION_DURATION,
                                        })
                                    )
                                except Exception:
                                    pass
                        # Eat food (increase mass)
                        eaten_food_ids.append(fid)
                        del foods[fid]
                        m = mass_from_radius(p["r"]) + float(
                            f.get("value", FOOD_VALUE)
                        )
                        p["r"] = radius_from_mass(m)
        if eaten_food_ids:
            spawn_food()

        # Handle player collisions (simple O(n^2))
        pids = list(players.keys())
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                a = players.get(pids[i])
                b = players.get(pids[j])
                if not a or not b:
                    continue
                dx = a["x"] - b["x"]
                dy = a["y"] - b["y"]
                dist2 = dx * dx + dy * dy
                if dist2 <= (a["r"] + b["r"]) ** 2:
                    inv_a = a.get("invUntil", 0) > now
                    inv_b = b.get("invUntil", 0) > now
                    if a["r"] > b["r"] and not inv_b:
                        m = mass_from_radius(a["r"]) + mass_from_radius(b["r"])
                        a["r"] = radius_from_mass(m)
                        await eliminate_player(b["id"], killer=a["id"])
                    elif b["r"] > a["r"] and not inv_a:
                        m = mass_from_radius(b["r"]) + mass_from_radius(a["r"])
                        b["r"] = radius_from_mass(m)
                        await eliminate_player(a["id"], killer=b["id"])

        # Broadcast state
        state = {
            "type": "state",
            "players": list(players.values()),
            "foods": list(foods.values())
        }
        msg = json.dumps(state)
        dead_connections = []
        for pid, ws in connections.items():
            try:
                await ws.send_text(msg)
            except Exception:
                dead_connections.append(pid)
        for d in dead_connections:
            await disconnect_player(d)

        await asyncio.sleep(TICK_RATE)


async def eliminate_player(pid: str, killer: str):
    if pid in players:
        ws = connections.get(pid)
        killer_player = players.get(killer)
        killer_name = (
            killer_player.get("name") if killer_player else None
        ) or "Guest"
        if ws:
            try:
                await ws.send_text(
                    json.dumps({
                        "type": "dead",
                        "killer": killer,
                        "killerName": killer_name
                    })
                )
            except Exception:
                pass
        # Lưu tên rồi xoá player, respawn sau delay với tên cũ
        old_name = players[pid].get("name", "")
        del players[pid]
        asyncio.create_task(respawn_player(pid, old_name))


async def respawn_player(pid: str, name: str):
    await asyncio.sleep(RESPAWN_DELAY)
    # Chỉ respawn nếu kết nối vẫn còn và chưa tồn tại player
    if pid in connections and pid not in players:
        players[pid] = new_player(pid, respawn=True, name=name)


def sanitize_name(name: str) -> str:
    name = name.strip()
    # Giữ lại chữ cái, số, khoảng trắng, gạch dưới
    name = re.sub(r"[^\w\s]", "", name)
    if len(name) > 16:
        name = name[:16]
    return name or "Guest"


def new_player(pid: str, respawn=False, name: str = ""):
    pos = random_position(INITIAL_RADIUS)
    return {
        "id": pid,
        "name": name,  # giữ tên nếu có
        "x": pos["x"],
        "y": pos["y"],
        "r": RESPAWN_RADIUS if respawn else INITIAL_RADIUS,
        "color": f"hsl({random.randint(0,360)},70%,55%)",
        "target": None,
        "score": 0,
        # effect timers (perf_counter timestamps)
        "speedUntil": 0.0,
        "speedStacks": 0,
        "invUntil": 0.0,
        "attractUntil": 0.0,
        "attractRange": 0.0,
    }


async def disconnect_player(pid: str):
    if pid in players:
        del players[pid]
    if pid in connections:
        try:
            await connections[pid].close()
        except Exception:
            pass
        del connections[pid]


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    pid = os.urandom(6).hex()
    connections[pid] = ws
    players[pid] = new_player(pid)
    # Send init
    await ws.send_text(
        json.dumps({
            "type": "init",
            "id": pid,
            "map": {"w": MAP_WIDTH, "h": MAP_HEIGHT}
        })
    )

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "move":
                p = players.get(pid)
                if p:
                    p["target"] = (float(msg.get("x")), float(msg.get("y")))
            elif mtype == "set_name":
                p = players.get(pid)
                if p:
                    p["name"] = sanitize_name(str(msg.get("name", "")))
    except WebSocketDisconnect:
        await disconnect_player(pid)
    except Exception:
        await disconnect_player(pid)
