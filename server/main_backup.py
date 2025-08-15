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

# Boost constants
BOOST_SPEED_MULTIPLIER = 2.0  # tốc độ tăng lên khi boost
BOOST_MASS_COST = 0.5  # khối lượng mất mỗi giây khi boost
MIN_RADIUS_FOR_BOOST = 10  # bán kính tối thiểu để có thể boost

# Arrow constants
ARROW_SPEED = 800  # tốc độ mũi tên
ARROW_LIFETIME = 3.0  # thời gian sống của mũi tên (giây)
ARROW_SIZE = 3  # kích thước mũi tên

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
arrows: Dict[int, dict] = {}  # dictionary để lưu mũi tên
connections: Dict[str, WebSocket] = {}

next_food_id = 0
next_arrow_id = 0

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


def drop_food_at_death(x, y, radius):
    """Tạo thức ăn tại vị trí người chơi chết"""
    global next_food_id
    mass = mass_from_radius(radius)
    # Tạo nhiều miếng thức ăn nhỏ từ khối lượng của người chơi
    num_pieces = min(20, max(5, int(radius / 3)))  # 5-20 miếng
    mass_per_piece = mass / num_pieces
    
    for _ in range(num_pieces):
        fid = next_food_id
        next_food_id += 1
        
        # Tạo vị trí ngẫu nhiên xung quanh vị trí chết
        angle = random.uniform(0, 2 * math.pi)
        distance = random.uniform(0, radius * 1.5)
        fx = max(10, min(MAP_WIDTH - 10,
                         x + distance * math.cos(angle)))
        fy = max(10, min(MAP_HEIGHT - 10,
                         y + distance * math.sin(angle)))
        
        # Kích thước dựa trên khối lượng
        food_radius = max(FOOD_MIN_R,
                          min(FOOD_MAX_R,
                              radius_from_mass(mass_per_piece)))
        
        foods[fid] = {
            "id": fid,
            "x": fx,
            "y": fy,
            "r": food_radius,
            "value": mass_per_piece,
            "type": "normal",
            "color": "#ffd54f",
        }


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
            
            # Handle boost (tiêu tốn khối lượng)
            if p.get("boosting", False):
                if p["r"] > MIN_RADIUS_FOR_BOOST:
                    # Giảm khối lượng
                    current_mass = mass_from_radius(p["r"])
                    mass_cost = BOOST_MASS_COST * dt
                    min_mass = mass_from_radius(MIN_RADIUS_FOR_BOOST)
                    new_mass = max(min_mass, current_mass - mass_cost)
                    p["r"] = radius_from_mass(new_mass)
                else:
                    p["boosting"] = False
            
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
                
                # Apply boost multiplier
                if p.get("boosting", False):
                    speed_mul *= BOOST_SPEED_MULTIPLIER
                
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

        # Update arrows
        expired_arrows = []
        for aid, arrow in list(arrows.items()):
            arrow["x"] += arrow["vx"] * dt
            arrow["y"] += arrow["vy"] * dt
            arrow["timeLeft"] -= dt
            
            # Check if arrow is out of bounds or expired
            if (arrow["timeLeft"] <= 0 or
                arrow["x"] < 0 or arrow["x"] > MAP_WIDTH or
                    arrow["y"] < 0 or arrow["y"] > MAP_HEIGHT):
                expired_arrows.append(aid)
                continue
            
            # Check arrow collision with players
            for pid, p in list(players.items()):
                if pid == arrow["shooter"]:  # không bắn chính mình
                    continue
                    
                # Check if arrow hits player
                dx = arrow["x"] - p["x"]
                dy = arrow["y"] - p["y"]
                dist = math.hypot(dx, dy)
                
                if dist <= p["r"] + ARROW_SIZE:
                    # Player hit by arrow - eliminate them
                    shooter_player = players.get(arrow["shooter"])
                    
                    # Drop food at death location
                    drop_food_at_death(p["x"], p["y"], p["r"])
                    
                    # Give mass to shooter
                    if shooter_player:
                        shooter_mass = mass_from_radius(shooter_player["r"])
                        victim_mass = mass_from_radius(p["r"])
                        new_mass = shooter_mass + victim_mass * 0.5
                        shooter_player["r"] = radius_from_mass(new_mass)
                    
                    await eliminate_player(pid, killer=arrow["shooter"])
                    expired_arrows.append(aid)
                    break
        
        # Remove expired arrows
        for aid in expired_arrows:
            if aid in arrows:
                del arrows[aid]

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
                            # Cung cấp thêm mũi tên (1-3 mũi tên tùy kích thước)
                            ammo_gain = max(1, int(area_ratio * 3))
                            current_ammo = p.get("shieldAmmo", 0)
                            p["shieldAmmo"] = current_ammo + ammo_gain
                            # notify player: chỉ cập nhật góc phải
                            ws = connections.get(pid)
                            if ws:
                                try:
                                    await ws.send_text(
                                        json.dumps({
                                            "type": "effect",
                                            "effect": "shield",
                                            "duration": duration,
                                            "ammo": p["shieldAmmo"],
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
            "foods": list(foods.values()),
            "arrows": list(arrows.values())
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
        "boosting": False,  # trạng thái tăng tốc
        "shieldAmmo": 0,  # số lượng mũi tên có thể bắn
        # effect timers (perf_counter timestamps)
        "speedUntil": 0.0,
        "speedStacks": 0,
        "invUntil": 0.0,
        "attractUntil": 0.0,
        "attractRange": 0.0,
    }


def create_arrow(shooter_id: str, target_x: float, target_y: float):
    """Tạo mũi tên từ player đến target"""
    global next_arrow_id
    shooter = players.get(shooter_id)
    if not shooter or shooter.get("shieldAmmo", 0) <= 0:
        return None
    
    # Tiêu tốn 1 mũi tên
    shooter["shieldAmmo"] -= 1
    
    # Tính hướng bắn
    dx = target_x - shooter["x"]
    dy = target_y - shooter["y"]
    dist = math.hypot(dx, dy)
    
    if dist < 1e-3:  # tránh chia cho 0
        return None
    
    # Tạo mũi tên
    arrow_id = next_arrow_id
    next_arrow_id += 1
    
    # Vận tốc đơn vị
    vx = (dx / dist) * ARROW_SPEED
    vy = (dy / dist) * ARROW_SPEED
    
    # Vị trí khởi đầu (từ rìa của player)
    start_x = shooter["x"] + (dx / dist) * shooter["r"]
    start_y = shooter["y"] + (dy / dist) * shooter["r"]
    
    arrow = {
        "id": arrow_id,
        "x": start_x,
        "y": start_y,
        "vx": vx,
        "vy": vy,
        "shooter": shooter_id,
        "timeLeft": ARROW_LIFETIME
    }
    
    arrows[arrow_id] = arrow
    return arrow


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
            elif mtype == "boost":
                p = players.get(pid)
                if p:
                    p["boosting"] = bool(msg.get("active", False))
            elif mtype == "shoot":
                p = players.get(pid)
                if p:
                    target_x = float(msg.get("x", p["x"]))
                    target_y = float(msg.get("y", p["y"]))
                    create_arrow(pid, target_x, target_y)
    except WebSocketDisconnect:
        await disconnect_player(pid)
    except Exception:
        await disconnect_player(pid)
