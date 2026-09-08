"""
route_model.py  —  TPSMC Lightweight ML Route Engine
═════════════════════════════════════════════════════
• Builds a weighted graph between nearby junctions
• Dijkstra for shortest-distance path
• Neural-net-style safety scorer for safest route
• No sklearn/torch dependency — pure Python + math
"""

import math
import heapq

# ── Haversine distance (meters) ──────────────────────────────────
def haversine(lat1, lng1, lat2, lng2):
    R = 6371000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Pre-trained safety weights (simulates a trained 2-layer MLP) ─
# These weights were "trained" on Bengaluru traffic patterns.
# Input features: [congestion_norm, alert_severity, crowd_norm, aqi_norm, noise_norm]
# Layer 1: 5 inputs → 4 hidden (ReLU)
# Layer 2: 4 hidden → 1 output (sigmoid → risk 0-1)
LAYER1_W = [
    [0.42, 0.68, 0.35, 0.22, 0.15],   # hidden neuron 0: congestion-heavy
    [0.18, 0.85, 0.55, 0.12, 0.08],   # hidden neuron 1: alert-heavy
    [0.30, 0.20, 0.60, 0.45, 0.30],   # hidden neuron 2: crowd+pollution
    [0.25, 0.15, 0.20, 0.55, 0.48],   # hidden neuron 3: environmental
]
LAYER1_B = [-0.3, -0.4, -0.35, -0.25]

LAYER2_W = [0.65, 0.50, 0.25, 0.18]
LAYER2_B = -0.1


def _relu(x):
    return max(0.0, x)


def _sigmoid(x):
    x = max(-10, min(10, x))
    return 1.0 / (1.0 + math.exp(-x))


def predict_safety_risk(congestion, alert_severity, crowd, aqi, noise):
    """
    Neural-net-style safety risk prediction.
    Returns risk score 0.0 (safe) to 1.0 (dangerous).

    Args:
        congestion: 0-100 congestion score
        alert_severity: 0-1 (0=CLEAR, 0.5=WARNING, 1.0=CRITICAL)
        crowd: 0-100 crowd percent
        aqi: 0-500 AQI value
        noise: 0-120 noise dB
    """
    # Normalize inputs to 0-1
    features = [
        min(1.0, congestion / 100.0),
        alert_severity,
        min(1.0, crowd / 100.0),
        min(1.0, aqi / 300.0),
        min(1.0, noise / 100.0),
    ]

    # Layer 1: hidden = ReLU(W1 · x + b1)
    hidden = []
    for i in range(4):
        z = LAYER1_B[i]
        for j in range(5):
            z += LAYER1_W[i][j] * features[j]
        hidden.append(_relu(z))

    # Layer 2: output = sigmoid(W2 · hidden + b2)
    z_out = LAYER2_B
    for i in range(4):
        z_out += LAYER2_W[i] * hidden[i]

    return _sigmoid(z_out)


# ── Alert severity mapping ───────────────────────────────────────
ALERT_SEVERITY = {
    "CLEAR": 0.0,
    "OFFLINE": 0.1,
    "CROWD_WARNING": 0.4,
    "POLLUTION_ALERT": 0.5,
    "EMERGENCY_SIREN": 0.85,
    "ACCIDENT_CRITICAL": 1.0,
}


# ── Build junction graph ─────────────────────────────────────────
def build_graph(junction_coords, max_edge_km=8.0):
    """
    Build adjacency list connecting junctions within max_edge_km.
    Returns: {junction_id: [(neighbor_id, distance_m), ...]}
    """
    graph = {jid: [] for jid in junction_coords}
    ids = list(junction_coords.keys())

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            j1, j2 = junction_coords[ids[i]], junction_coords[ids[j]]
            dist = haversine(j1["lat"], j1["lng"], j2["lat"], j2["lng"])
            if dist <= max_edge_km * 1000:
                graph[ids[i]].append((ids[j], dist))
                graph[ids[j]].append((ids[i], dist))

    return graph


# ── Dijkstra's algorithm ─────────────────────────────────────────
def dijkstra(graph, weight_fn, start, end):
    """
    Find shortest path using custom weight function.
    weight_fn(neighbor_id, distance_m) → weighted cost

    Returns: (total_cost, [path_ids]) or (inf, []) if unreachable
    """
    dist = {node: float("inf") for node in graph}
    prev = {node: None for node in graph}
    dist[start] = 0
    pq = [(0, start)]

    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u == end:
            break
        for neighbor, edge_dist in graph[u]:
            cost = weight_fn(u, neighbor, edge_dist)
            new_dist = dist[u] + cost
            if new_dist < dist[neighbor]:
                dist[neighbor] = new_dist
                prev[neighbor] = u
                heapq.heappush(pq, (new_dist, neighbor))

    # Reconstruct path
    if dist[end] == float("inf"):
        return float("inf"), []

    path = []
    node = end
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()
    return dist[end], path


# ── Main route suggestion API ────────────────────────────────────
def suggest_routes(junction_coords, live_junction_data, origin_id, destination_id):
    """
    Compute shortest and safest routes between two junctions.

    Args:
        junction_coords: {id: {lat, lng, name}}
        live_junction_data: {id: {congestion_score, alert, crowd_percent, aqi, noise_db, ...}}
        origin_id: start junction ID
        destination_id: end junction ID

    Returns: dict with shortest_route, safest_route, and ML predictions
    """
    if origin_id not in junction_coords or destination_id not in junction_coords:
        return {"error": "Invalid junction ID", "valid_ids": list(junction_coords.keys())[:20]}

    graph = build_graph(junction_coords)

    # Check connectivity
    if not graph.get(origin_id) or not graph.get(destination_id):
        return {"error": "Junction has no connections in graph"}

    # ── Route 1: Shortest (distance only) ──
    def weight_distance(from_nid, to_nid, dist_m):
        return dist_m

    shortest_cost, shortest_path = dijkstra(graph, weight_distance, origin_id, destination_id)

    # ── Route 2: Safest (ML risk-weighted) ──
    def weight_safety(from_nid, to_nid, dist_m):
        # Apply risk of BOTH the node we're leaving AND the node we're entering.
        # This causes Dijkstra to avoid paths that pass THROUGH dangerous nodes.
        def node_risk(nid):
            jdata = live_junction_data.get(nid, {})
            return predict_safety_risk(
                jdata.get("congestion_score", 0),
                ALERT_SEVERITY.get(jdata.get("alert", "OFFLINE"), 0.1),
                jdata.get("crowd_percent", 0),
                jdata.get("aqi", 0),
                jdata.get("noise_db", 0),
            )

        from_risk = node_risk(from_nid)
        to_risk   = node_risk(to_nid)
        # Average risk of traversing this edge
        edge_risk = (from_risk + to_risk) / 2.0

        # Penalty: up to 20km equivalent per edge — forces detour around dangerous zones
        risk_penalty = edge_risk * 20000
        return dist_m * 0.25 + risk_penalty * 0.75

    safest_cost, safest_path = dijkstra(graph, weight_safety, origin_id, destination_id)

    # ── Build response ──
    def path_details(path):
        details = []
        total_dist = 0
        total_risk = 0
        for jid in path:
            jcoord = junction_coords.get(jid, {})
            jdata = live_junction_data.get(jid, {})
            congestion = jdata.get("congestion_score", 0)
            alert = jdata.get("alert", "OFFLINE")
            crowd = jdata.get("crowd_percent", 0)
            aqi = jdata.get("aqi", 0)
            noise = jdata.get("noise_db", 0)

            risk = predict_safety_risk(
                congestion,
                ALERT_SEVERITY.get(alert, 0.1),
                crowd, aqi, noise,
            )
            risk_label = "SAFE" if risk < 0.35 else "MODERATE" if risk < 0.65 else "RISKY"
            total_risk += risk

            details.append({
                "id": jid,
                "name": jcoord.get("name", jid),
                "lat": jcoord.get("lat"),
                "lng": jcoord.get("lng"),
                "risk_score": round(risk, 3),
                "risk_label": risk_label,
                "congestion": congestion,
                "alert": alert,
            })

        # Calculate distances between consecutive points
        for i in range(1, len(path)):
            p = junction_coords[path[i - 1]]
            c = junction_coords[path[i]]
            total_dist += haversine(p["lat"], p["lng"], c["lat"], c["lng"])

        avg_risk = round(total_risk / max(1, len(path)), 3)
        safety_score = int(max(0, min(100, (1 - avg_risk) * 100)))

        return {
            "path": details,
            "total_distance_m": round(total_dist),
            "total_distance_km": round(total_dist / 1000, 2),
            "avg_risk": avg_risk,
            "safety_score": safety_score,
            "hops": len(path),
        }

    shortest_details = path_details(shortest_path) if shortest_path else None
    safest_details = path_details(safest_path) if safest_path else None

    # ML verdict
    if shortest_details and safest_details:
        if shortest_path == safest_path:
            verdict = "SAME_ROUTE"
            reason = "Shortest path is also the safest — no active hazards detected."
        elif safest_details["safety_score"] - (shortest_details.get("safety_score", 0)) >= 15:
            verdict = "TAKE_SAFEST"
            reason = (
                f"Safest route is {safest_details['safety_score']}/100 safe vs "
                f"{shortest_details['safety_score']}/100 for shortest. "
                f"Extra {safest_details['total_distance_km'] - shortest_details['total_distance_km']:.1f}km "
                f"but avoids high-risk zones."
            )
        else:
            verdict = "SHORTEST_OK"
            reason = "Both routes have similar safety. Shortest path recommended."
    else:
        verdict = "NO_ROUTE"
        reason = "No valid route found between these junctions."

    return {
        "status": "ok",
        "origin": origin_id,
        "destination": destination_id,
        "model": "TPSMC-RouteNet v1.0 (2-layer MLP safety scorer + Dijkstra)",
        "ml_verdict": verdict,
        "ml_reason": reason,
        "shortest_route": shortest_details,
        "safest_route": safest_details,
    }

# ── Junction safety ranking ──────────────────────────────────────
def rank_junctions(junction_coords, live_junction_data):
    """
    Rank all junctions by ML safety risk (worst first).
    Returns list of dicts for dashboard display.
    """
    results = []
    for jid, jcoord in junction_coords.items():
        jdata = live_junction_data.get(jid, {})
        congestion = jdata.get("congestion_score", 0)
        alert      = jdata.get("alert", "OFFLINE")
        crowd      = jdata.get("crowd_percent", 0)
        aqi        = jdata.get("aqi", 0)
        noise      = jdata.get("noise_db", 0)

        risk = predict_safety_risk(
            congestion,
            ALERT_SEVERITY.get(alert, 0.1),
            crowd, aqi, noise,
        )
        risk_label = "SAFE" if risk < 0.35 else "MODERATE" if risk < 0.65 else "RISKY"

        results.append({
            "id": jid,
            "name": jcoord.get("name", jid),
            "lat": jcoord.get("lat"),
            "lng": jcoord.get("lng"),
            "risk_score": round(risk, 3),
            "risk_label": risk_label,
            "safety_score": int((1 - risk) * 100),
            "congestion": congestion,
            "alert": alert,
            "crowd_percent": crowd,
            "aqi": aqi,
        })

    results.sort(key=lambda x: x["risk_score"], reverse=True)
    return results
