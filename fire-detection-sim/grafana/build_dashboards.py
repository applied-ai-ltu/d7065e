"""Build the two D7065E Grafana dashboards as JSON (grafana/operations.json, grafana/technical.json).

Operations: what a facility operator needs during an incident — controller health, confirmed
fires, rooms on watch, door state, incident timeline, live readings, Tier-1/Tier-2 decisions.
Technical: how the two tiers behave — IF latency per node, alert->verdict latency, heartbeat
health, IF-vs-LLM agreement, pipeline throughput.

Datasource UID is a placeholder; scripts/grafana_provision.py resolves it by name at push time.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
S = "fire_detection_sim"
DS = {"type": "grafana-postgresql-datasource", "uid": "DS_TIMESCALE"}
FLOOR = "level IN (${floor:singlequote})"
NOT_META = r"room NOT LIKE '\_%'"

_ids = iter(range(1, 1000))


# ------------------------------------------------------------------ helpers
def target(sql, fmt="time_series", ref="A"):
    return {
        "refId": ref,
        "datasource": DS,
        "format": fmt,
        "rawQuery": True,
        "editorMode": "code",
        "rawSql": " ".join(sql.split()),
    }


def panel(
    ptype,
    title,
    x,
    y,
    w,
    h,
    targets,
    *,
    options=None,
    defaults=None,
    overrides=None,
    transformations=None,
    description=None,
):
    p = {
        "id": next(_ids),
        "type": ptype,
        "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": defaults or {}, "overrides": overrides or []},
        "options": options or {},
    }
    if transformations:
        p["transformations"] = transformations
    if description:
        p["description"] = description
    return p


def stat(title, x, y, w, sql, *, unit=None, thresholds=None, decimals=None, h=4, desc=None):
    defaults = {"color": {"mode": "thresholds"}, "noValue": "none in window"}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return panel(
        "stat",
        title,
        x,
        y,
        w,
        h,
        [target(sql)],
        options={
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value_and_name",
            "justifyMode": "center",
        },
        defaults=defaults,
        description=desc,
    )


def timeseries(
    title,
    x,
    y,
    w,
    h,
    targets,
    *,
    unit=None,
    stacked=False,
    bars=False,
    points=False,
    desc=None,
    overrides=None,
):
    custom = {"lineWidth": 1, "fillOpacity": 25 if (stacked or bars) else 10, "showPoints": "never"}
    if stacked:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    if bars:
        custom["drawStyle"] = "bars"
    if points:
        custom.update({"drawStyle": "points", "showPoints": "always", "pointSize": 6})
    defaults = {"custom": custom, "color": {"mode": "palette-classic"}}
    if unit:
        defaults["unit"] = unit
    return panel(
        "timeseries",
        title,
        x,
        y,
        w,
        h,
        targets,
        options={
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        defaults=defaults,
        description=desc,
        overrides=overrides,
    )


def table(title, x, y, w, h, sql, *, overrides=None, desc=None):
    return panel(
        "table",
        title,
        x,
        y,
        w,
        h,
        [target(sql, "table")],
        options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
        defaults={"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "filterable": True}},
        overrides=overrides,
        description=desc,
    )


def bargauge(title, x, y, w, h, sql, *, desc=None, unit=None):
    defaults = {"color": {"mode": "palette-classic"}}
    if unit:
        defaults["unit"] = unit
    return panel(
        "bargauge",
        title,
        x,
        y,
        w,
        h,
        [target(sql)],
        options={
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "showUnfilled": True,
            "namePlacement": "left",
            "sizing": "auto",
        },
        defaults=defaults,
        description=desc,
    )


def bool_override(field, true_text, false_text):
    return {
        "matcher": {"id": "byName", "options": field},
        "properties": [
            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
            {
                "id": "mappings",
                "value": [
                    {
                        "type": "value",
                        "options": {
                            "true": {"text": true_text, "color": "green", "index": 0},
                            "false": {"text": false_text, "color": "red", "index": 1},
                        },
                    }
                ],
            },
        ],
    }


def flagged_readings(sensor_type, title, x, y, w, h, unit=None):
    """Live readings for the rooms Tier 1 flagged (any decision) in the window."""
    return timeseries(
        title,
        x,
        y,
        w,
        h,
        [
            target(f"""
            WITH hot AS (
              SELECT DISTINCT room, level FROM {S}.edge_decisions
              WHERE $__timeFilter(time) AND {NOT_META} AND {FLOOR})
            SELECT time_bucket('10 seconds', r.time) AS time, r.level || '/' || r.room AS metric,
                   AVG(r.value)::float AS value
            FROM {S}.sensor_readings r JOIN hot USING (room, level)
            WHERE r.sensor_type = '{sensor_type}' AND $__timeFilter(r.time)
            GROUP BY 1, 2 ORDER BY 1""")
        ],
        unit=unit,
        desc="Rooms that produced a Tier-1 decision in the selected window",
    )


VERDICT_MAPPINGS = [
    {
        "type": "value",
        "options": {
            "confirm": {"text": "confirmed", "color": "red", "index": 0},
            "escalated": {"text": "escalated", "color": "dark-red", "index": 1},
            "stand_down_refused": {"text": "stand-down refused", "color": "purple", "index": 2},
            "watch_pending": {"text": "watching", "color": "orange", "index": 3},
            "stand_down": {"text": "stood down", "color": "green", "index": 4},
        },
    }
]


def annotations(kinds, color="red"):
    return {
        "list": [
            {
                "builtIn": 1,
                "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "enable": True,
                "hide": True,
                "iconColor": "rgba(0, 211, 255, 1)",
                "name": "Annotations & Alerts",
                "type": "dashboard",
            },
            {
                "datasource": DS,
                "enable": True,
                "hide": False,
                "iconColor": color,
                "name": "Tier-2 events",
                "target": {
                    "format": "table",
                    "rawQuery": True,
                    "editorMode": "code",
                    "rawSql": f"SELECT time, override_type || ' ' || level || '/' || room AS text, "
                    f"override_type AS tags FROM {S}.llm_overrides WHERE $__timeFilter(time) "
                    f"AND override_type IN ({','.join(repr(k) for k in kinds)})",
                },
            },
        ]
    }


def dashboard(
    uid, title, panels, *, refresh, time_from, templating=None, annotations_=None, tags=()
):
    return {
        "uid": uid,
        "title": title,
        "tags": ["d7065e", *tags],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": refresh,
        "time": {"from": time_from, "to": "now"},
        "templating": {"list": templating or []},
        "annotations": annotations_ or {"list": []},
        "panels": panels,
        "schemaVersion": 39,
        "version": 1,
    }


FLOOR_VAR = {
    "name": "floor",
    "label": "Floor",
    "type": "custom",
    "multi": True,
    "includeAll": True,
    "allValue": "'level0','level1','level2'",
    "query": "level0,level1,level2",
    "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
    "options": [
        {"selected": True, "text": "All", "value": "$__all"},
        {"selected": False, "text": "level0", "value": "level0"},
        {"selected": False, "text": "level1", "value": "level1"},
        {"selected": False, "text": "level2", "value": "level2"},
    ],
}


# --------------------------------------------------------------- operations
def build_operations():
    last_state = f"""
        SELECT DISTINCT ON (room) room, level, override_type FROM {S}.llm_overrides
        WHERE {NOT_META} AND time > NOW() - INTERVAL '3 hours'
          AND override_type IN ('confirm','escalated','stand_down_refused','watch_pending','stand_down')
        ORDER BY room, time DESC"""
    panels = [
        stat(
            "Edge controllers alive",
            0,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'of 6' AS metric, COUNT(*)::float AS value FROM (
              SELECT level, role FROM {S}.heartbeats WHERE time > NOW() - INTERVAL '30 seconds'
              GROUP BY level, role) h""",
            thresholds=[
                {"color": "red", "value": None},
                {"color": "orange", "value": 3},
                {"color": "green", "value": 6},
            ],
            desc="Controllers (active+standby per floor) that sent a heartbeat in the last 30 s",
        ),
        stat(
            "Confirmed fires",
            4,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'rooms' AS metric, COUNT(*)::float AS value FROM ({last_state}) r
            WHERE override_type IN ('confirm','escalated','stand_down_refused')""",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            desc="Rooms whose latest Tier-2 verdict is confirmed / escalated / stand-down refused",
        ),
        stat(
            "Rooms on watch",
            8,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'rooms' AS metric, COUNT(*)::float AS value FROM ({last_state}) r
            WHERE override_type = 'watch_pending'""",
            thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
            desc="LLM doubted the edge alert; readings watched for 60 s before any stand-down",
        ),
        stat(
            "Floors with doors released",
            12,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'floors' AS metric, COUNT(*)::float AS value FROM (
              SELECT DISTINCT ON (level) level, decision FROM {S}.edge_decisions
              WHERE room = '_doors' AND time > NOW() - INTERVAL '3 hours' ORDER BY level, time DESC) d
            WHERE decision = 'doors_released'""",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            desc="Latest door action reported by each floor controller",
        ),
        stat(
            "Server takeover",
            16,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'floors' AS metric, COUNT(*)::float AS value FROM (
              SELECT DISTINCT ON (level) level, override_type FROM {S}.llm_overrides
              WHERE override_type IN ('server_takeover','server_handback')
                AND time > NOW() - INTERVAL '3 hours' ORDER BY level, time DESC) t
            WHERE override_type = 'server_takeover'""",
            thresholds=[{"color": "green", "value": None}, {"color": "purple", "value": 1}],
            desc="Floors where both controllers are silent and the server is covering",
        ),
        stat(
            "Sensor feed (rooms/min)",
            20,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'rooms' AS metric, COUNT(DISTINCT room)::float AS value
            FROM {S}.sensor_readings WHERE time > NOW() - INTERVAL '1 minute' AND {FLOOR}""",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        table(
            "Edge controllers",
            0,
            4,
            12,
            7,
            f"""
            SELECT level, role, node,
                   EXTRACT(EPOCH FROM NOW() - MAX(time)) < 30 AS alive,
                   BOOL_OR(acting) FILTER (WHERE time > NOW() - INTERVAL '30 seconds') AS acting,
                   MAX(time) AS last_seen
            FROM {S}.heartbeats WHERE time > NOW() - INTERVAL '1 hour'
            GROUP BY level, role, node ORDER BY level, role""",
            overrides=[
                bool_override("alive", "alive", "SILENT"),
                bool_override("acting", "ACTING", "standby"),
            ],
            desc="Active on the Raspberry Pi, standby on the amd64 node. A standby promotes after 30 s of silence.",
        ),
        table(
            "Door state per floor",
            12,
            4,
            12,
            7,
            f"""
            SELECT DISTINCT ON (level) level, node, decision AS doors, time
            FROM {S}.edge_decisions WHERE room = '_doors' AND time > NOW() - INTERVAL '3 hours'
            ORDER BY level, time DESC""",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "doors"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {
                            "id": "mappings",
                            "value": [
                                {
                                    "type": "value",
                                    "options": {
                                        "doors_released": {
                                            "text": "RELEASED",
                                            "color": "red",
                                            "index": 0,
                                        },
                                        "doors_locked": {
                                            "text": "locked",
                                            "color": "green",
                                            "index": 1,
                                        },
                                    },
                                }
                            ],
                        },
                    ],
                }
            ],
        ),
        panel(
            "state-timeline",
            "Incident timeline (Tier-2 verdict per room)",
            0,
            11,
            24,
            8,
            [
                target(f"""
                SELECT time, level || '/' || room AS metric,
                       CASE override_type WHEN 'stand_down' THEN 1 WHEN 'watch_pending' THEN 2
                            WHEN 'stand_down_refused' THEN 3 WHEN 'confirm' THEN 4
                            WHEN 'escalated' THEN 5 END::float AS value
                FROM {S}.llm_overrides WHERE $__timeFilter(time) AND {NOT_META} AND {FLOOR}
                  AND override_type IN ('confirm','escalated','stand_down_refused','watch_pending','stand_down')
                ORDER BY time""")
            ],
            options={
                "mergeValues": True,
                "showValue": "auto",
                "alignValue": "left",
                "rowHeight": 0.8,
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            },
            defaults={
                # NOT "thresholds": in a state timeline that scheme turns threshold bands into
                # the states themselves (labelled "-inf..+inf") and ignores value mappings.
                "color": {"mode": "fixed", "fixedColor": "green"},
                # range mappings: numeric, so they match however the datasource types the value
                "mappings": [
                    {
                        "type": "range",
                        "options": {
                            "from": v - 0.5,
                            "to": v + 0.5,
                            "result": {"text": t, "color": c, "index": i},
                        },
                    }
                    for i, (v, t, c) in enumerate(
                        [
                            (1, "stood down", "green"),
                            (2, "watching", "orange"),
                            (3, "stand-down refused", "purple"),
                            (4, "confirmed", "red"),
                            (5, "escalated", "dark-red"),
                        ]
                    )
                ],
                "custom": {"fillOpacity": 80, "lineWidth": 0},
            },
            description="Escalate immediately, de-escalate only on 60 s of clear readings",
        ),
        flagged_readings("smoke", "Smoke — rooms flagged by Tier 1", 0, 19, 8, 8, unit="percent"),
        flagged_readings("co", "CO — rooms flagged by Tier 1", 8, 19, 8, 8, unit="ppm"),
        flagged_readings(
            "temperature", "Temperature — rooms flagged by Tier 1", 16, 19, 8, 8, unit="celsius"
        ),
        timeseries(
            "Tier 1 — edge decisions (per 30 s)",
            0,
            27,
            12,
            8,
            [
                target(f"""
            SELECT time_bucket('30 seconds', time) AS time, decision AS metric, COUNT(*)::float AS value
            FROM {S}.edge_decisions WHERE $__timeFilter(time) AND kind = 'decision' AND {NOT_META}
              AND {FLOOR} GROUP BY 1, 2 ORDER BY 1""")
            ],
            stacked=True,
            bars=True,
            desc="normal = threshold candidate rejected by the IF; possible/fire = escalated to the server; clear = room back below thresholds",
        ),
        timeseries(
            "Tier 2 — LLM verdicts (per 30 s)",
            12,
            27,
            12,
            8,
            [
                target(f"""
            SELECT time_bucket('30 seconds', time) AS time, override_type AS metric, COUNT(*)::float AS value
            FROM {S}.llm_overrides WHERE $__timeFilter(time) AND {NOT_META} AND {FLOOR}
            GROUP BY 1, 2 ORDER BY 1""")
            ],
            stacked=True,
            bars=True,
        ),
        table(
            "Incident log (Tier 2)",
            0,
            35,
            12,
            9,
            f"""
            SELECT time, level || '/' || room AS location, override_type AS event, reason
            FROM {S}.llm_overrides WHERE $__timeFilter(time) AND {FLOOR}
            ORDER BY time DESC LIMIT 100""",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "event"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                        {"id": "mappings", "value": VERDICT_MAPPINGS},
                    ],
                }
            ],
        ),
        table(
            "Edge alerts (Tier 1)",
            12,
            35,
            12,
            9,
            f"""
            SELECT time, level || '/' || room AS location, node, severity,
                   ROUND(smoke::numeric, 1) AS smoke, ROUND(co::numeric, 1) AS co,
                   ROUND(temperature::numeric, 1) AS temp, if_anomaly, if_ms
            FROM {S}.edge_decisions WHERE kind = 'alert' AND $__timeFilter(time) AND {FLOOR}
            ORDER BY time DESC LIMIT 100""",
        ),
        table(
            "LLM assessments (fire_events)",
            0,
            44,
            24,
            7,
            f"""
            SELECT time, event_type, level || '/' || room AS location, severity, details::text
            FROM {S}.fire_events WHERE $__timeFilter(time) AND {FLOOR}
            ORDER BY time DESC LIMIT 30""",
        ),
    ]
    return dashboard(
        "731d813a-12da-44c0-97b4-91099ea1a1cc",
        "D7065E Fire Detection — Operations",
        panels,
        refresh="5s",
        time_from="now-15m",
        templating=[FLOOR_VAR],
        tags=["operations"],
        annotations_=annotations(
            ["confirm", "escalated", "stand_down", "failover", "server_takeover", "server_handback"]
        ),
    )


# ---------------------------------------------------------------- technical
def build_technical():
    alert_to_verdict = f"""
        SELECT o.time, o.room, o.override_type,
               (SELECT MAX(a.time) FROM {S}.edge_decisions a
                WHERE a.kind = 'alert' AND a.room = o.room
                  AND a.time BETWEEN o.time - INTERVAL '120 seconds' AND o.time) AS t_alert
        FROM {S}.llm_overrides o
        WHERE o.override_type IN ('confirm','watch_pending') AND $__timeFilter(o.time)"""
    panels = [
        stat(
            "IF latency p50 by node",
            0,
            0,
            6,
            f"""
            SELECT NOW() AS time, node AS metric,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY if_ms)::float AS value
            FROM {S}.edge_decisions WHERE if_ms IS NOT NULL AND $__timeFilter(time) GROUP BY node""",
            unit="ms",
            decimals=1,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 50},
                {"color": "red", "value": 100},
            ],
            desc="Isolation-forest ONNX inference per room, measured on the edge",
        ),
        stat(
            "IF latency p95 by node",
            6,
            0,
            6,
            f"""
            SELECT NOW() AS time, node AS metric,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY if_ms)::float AS value
            FROM {S}.edge_decisions WHERE if_ms IS NOT NULL AND $__timeFilter(time) GROUP BY node""",
            unit="ms",
            decimals=1,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 50},
                {"color": "red", "value": 100},
            ],
        ),
        stat(
            "Alert → LLM verdict p50",
            12,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'latency' AS metric,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM time - t_alert))::float AS value
            FROM ({alert_to_verdict}) v WHERE t_alert IS NOT NULL""",
            unit="s",
            decimals=1,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 10},
                {"color": "red", "value": 30},
            ],
            desc="Edge alert published -> Tier-2 verdict logged (includes LLM inference)",
        ),
        stat(
            "LLM inference (p50 / p95)",
            16,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'p50' AS metric,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY llm_ms)::float AS value
            FROM {S}.llm_overrides WHERE llm_ms IS NOT NULL AND $__timeFilter(time)
            UNION ALL
            SELECT NOW(), 'p95', percentile_cont(0.95) WITHIN GROUP (ORDER BY llm_ms)::float
            FROM {S}.llm_overrides WHERE llm_ms IS NOT NULL AND $__timeFilter(time)""",
            unit="ms",
            decimals=0,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 3000},
                {"color": "red", "value": 8000},
            ],
            desc="generate_json() wall time on the server (LiteLLM -> SGLang); excludes queueing",
        ),
        stat(
            "LLM assessments",
            20,
            0,
            4,
            f"""
            SELECT NOW() AS time, 'calls' AS metric, COUNT(*)::float AS value
            FROM {S}.llm_overrides WHERE override_type IN ('confirm','watch_pending') AND $__timeFilter(time)""",
            thresholds=[{"color": "blue", "value": None}],
        ),
        timeseries(
            "IF inference latency p95 by node (1 min)",
            0,
            4,
            8,
            8,
            [
                target(f"""
            SELECT time_bucket('1 minute', time) AS time, node AS metric,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY if_ms)::float AS value
            FROM {S}.edge_decisions WHERE if_ms IS NOT NULL AND $__timeFilter(time)
            GROUP BY 1, 2 ORDER BY 1""")
            ],
            unit="ms",
            desc="Pi 400 (arm64, active) vs the amd64 standby node vs the server (takeover)",
        ),
        timeseries(
            "Alert → LLM verdict latency",
            8,
            4,
            8,
            8,
            [
                target(f"""
            SELECT time, override_type AS metric, EXTRACT(EPOCH FROM time - t_alert)::float AS value
            FROM ({alert_to_verdict}) v WHERE t_alert IS NOT NULL ORDER BY time""")
            ],
            unit="s",
            points=True,
        ),
        timeseries(
            "LLM inference latency",
            16,
            4,
            8,
            8,
            [
                target(f"""
            SELECT time, override_type AS metric, llm_ms::float AS value
            FROM {S}.llm_overrides WHERE llm_ms IS NOT NULL AND $__timeFilter(time) ORDER BY time""")
            ],
            unit="ms",
            points=True,
            desc="Per call; compare before/after SGLang changes on identical prompts",
        ),
        timeseries(
            "Heartbeats per minute per controller",
            0,
            12,
            12,
            8,
            [
                target(f"""
            SELECT time_bucket('1 minute', time) AS time, level || '/' || role AS metric, COUNT(*)::float AS value
            FROM {S}.heartbeats WHERE $__timeFilter(time) GROUP BY 1, 2 ORDER BY 1""")
            ],
            desc="Expected 6/min per controller (10 s interval). A gap is an outage.",
        ),
        table(
            "Failover / takeover events",
            12,
            12,
            12,
            8,
            f"""
            SELECT time, level, override_type AS event, reason
            FROM {S}.llm_overrides
            WHERE override_type IN ('failover','server_takeover','server_handback') AND $__timeFilter(time)
            ORDER BY time DESC LIMIT 50""",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "event"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                        {
                            "id": "mappings",
                            "value": [
                                {
                                    "type": "value",
                                    "options": {
                                        "failover": {
                                            "text": "standby promoted",
                                            "color": "orange",
                                            "index": 0,
                                        },
                                        "server_takeover": {
                                            "text": "SERVER TAKEOVER",
                                            "color": "purple",
                                            "index": 1,
                                        },
                                        "server_handback": {
                                            "text": "handed back",
                                            "color": "green",
                                            "index": 2,
                                        },
                                    },
                                }
                            ],
                        },
                    ],
                }
            ],
        ),
        bargauge(
            "Tier-2 verdict on edge alerts",
            0,
            20,
            8,
            8,
            f"""
            WITH a AS (
              SELECT time, room FROM {S}.edge_decisions
              WHERE kind = 'alert' AND severity IN ('warning','confirmed','critical') AND $__timeFilter(time)),
            v AS (
              SELECT (SELECT o.override_type FROM {S}.llm_overrides o
                      WHERE o.room = a.room AND o.time BETWEEN a.time AND a.time + INTERVAL '60 seconds'
                      ORDER BY o.time LIMIT 1) AS llm FROM a)
            SELECT NOW() AS time,
                   CASE WHEN llm = 'confirm' THEN 'LLM confirmed'
                        WHEN llm = 'watch_pending' THEN 'LLM doubted'
                        ELSE 'not re-assessed (dedupe)' END AS metric,
                   COUNT(*)::float AS value
            FROM v GROUP BY 2 ORDER BY 2""",
            desc="What Tier 2 made of each Tier-1 alert",
        ),
        bargauge(
            "Tier-1 decision mix by node",
            8,
            20,
            8,
            8,
            f"""
            SELECT NOW() AS time, node || '  ' || decision AS metric, COUNT(*)::float AS value
            FROM {S}.edge_decisions WHERE kind = 'decision' AND {NOT_META} AND $__timeFilter(time)
            GROUP BY 2 ORDER BY 2""",
        ),
        bargauge(
            "Tier-2 verdict mix",
            16,
            20,
            8,
            8,
            f"""
            SELECT NOW() AS time, override_type AS metric, COUNT(*)::float AS value
            FROM {S}.llm_overrides WHERE $__timeFilter(time) GROUP BY 2 ORDER BY 2""",
        ),
        timeseries(
            "Pipeline throughput (rows/min)",
            0,
            28,
            12,
            8,
            [
                target(
                    f"""SELECT time_bucket('1 minute', time) AS time, 'sensor_readings' AS metric,
                       COUNT(*)::float AS value FROM {S}.sensor_readings WHERE $__timeFilter(time)
                       GROUP BY 1 ORDER BY 1""",
                    ref="A",
                ),
                target(
                    f"""SELECT time_bucket('1 minute', time) AS time, 'edge_decisions' AS metric,
                       COUNT(*)::float AS value FROM {S}.edge_decisions WHERE $__timeFilter(time)
                       GROUP BY 1 ORDER BY 1""",
                    ref="B",
                ),
                target(
                    f"""SELECT time_bucket('1 minute', time) AS time, 'heartbeats' AS metric,
                       COUNT(*)::float AS value FROM {S}.heartbeats WHERE $__timeFilter(time)
                       GROUP BY 1 ORDER BY 1""",
                    ref="C",
                ),
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "sensor_readings"},
                    "properties": [{"id": "custom.axisPlacement", "value": "right"}],
                }
            ],
        ),
        stat(
            "Table sizes (approx. rows)",
            12,
            28,
            12,
            f"""
            SELECT NOW() AS time, t AS metric, approximate_row_count('{S}.' || t)::float AS value
            FROM unnest(ARRAY['sensor_readings','edge_decisions','heartbeats','llm_overrides','fire_events']) AS t""",
            thresholds=[{"color": "blue", "value": None}],
            h=8,
        ),
    ]
    return dashboard(
        "tech-d7065e",
        "D7065E Technical — Edge & Pipeline",
        panels,
        refresh="10s",
        time_from="now-1h",
        tags=["technical"],
        annotations_=annotations(
            ["failover", "server_takeover", "server_handback"], color="purple"
        ),
    )


def main():
    for name, dash in [
        ("operations", build_operations()),
        ("technical", build_technical()),
    ]:
        out = HERE / f"{name}.json"
        out.write_text(json.dumps(dash, indent=2) + "\n")
        print(f"wrote {out} ({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
