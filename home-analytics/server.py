#!/usr/bin/env python3
"""Home Analytics Dashboard v2.0.0
Event-driven trend analysis with persistent daily summaries and anomaly detection.

v2.0 additions:
  - Event Bus SSE subscriber: builds stats database in real-time
  - Persistent daily summaries in /data/stats_db.json
  - Monthly/yearly trend queries
  - Anomaly detection: flag unusual patterns
  - Presence hours, departure counts, energy aggregation

Endpoints:
  GET /health
  GET /energy/today, /energy/week
  GET /climate/now, /climate/history/<hours>
  GET /sleep/last-night, /sleep/week
  GET /presence/today, /presence/patterns
  GET /devices/health, /batteries
  GET /summary
  GET /stats/daily         — Persistent daily summaries
  GET /stats/monthly       — Monthly aggregates
  GET /stats/anomalies     — Detected anomalies
  GET /event-log           — Recent event-driven collections
"""
import os, json, time, logging, threading
from datetime import datetime, timedelta, timezone
from collections import deque, defaultdict
from flask import Flask, jsonify, request
import requests as http
import sseclient

HA_URL = os.environ.get('HA_URL', 'http://localhost:8123')
HA_TOKEN = os.environ.get('HA_TOKEN', '')
API_PORT = int(os.environ.get('API_PORT', '8095'))
EVENT_BUS_URL = os.environ.get('EVENT_BUS_URL', 'http://localhost:8092')

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('home-analytics')

STATS_DB_FILE = '/data/stats_db.json'

# v2.0: In-memory stats accumulator (flushed to disk periodically)
# Structure: {"YYYY-MM-DD": {energy, climate, presence, vacuum, anomalies}}
stats_db = {}
today_stats = {}  # Live accumulator for today
event_actions = deque(maxlen=200)

# Counters for today
departure_count = 0
arrival_count = 0
motion_counts = defaultdict(int)  # entity_id -> count today
temp_readings = defaultdict(list)  # entity_id -> [values]


def load_stats_db():
    global stats_db
    try:
        if os.path.exists(STATS_DB_FILE):
            stats_db = json.load(open(STATS_DB_FILE))
            logger.info(f'Loaded stats DB: {len(stats_db)} days')
    except Exception as e:
        logger.error(f'Load stats DB: {e}')
        stats_db = {}


def save_stats_db():
    try:
        # Keep last 365 days
        cutoff = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        pruned = {k: v for k, v in stats_db.items() if k >= cutoff}
        json.dump(pruned, open(STATS_DB_FILE, 'w'), indent=2)
    except Exception as e:
        logger.error(f'Save stats DB: {e}')


def flush_today():
    """Write today's accumulated stats to the persistent DB."""
    today = datetime.now().strftime('%Y-%m-%d')
    stats_db[today] = {
        'date': today,
        'presence': {
            'arrivals': arrival_count,
            'departures': departure_count,
        },
        'motion': dict(motion_counts),
        'temperature_samples': {k: len(v) for k, v in temp_readings.items()},
        'temperature_avg': {k: round(sum(v) / len(v), 1) for k, v in temp_readings.items() if v},
        'event_actions': len(event_actions),
        'flushed_at': datetime.now().isoformat(),
    }
    save_stats_db()


def detect_anomaly(entity_id, value, label):
    """v2.0: Compare value against 30-day historical average."""
    # Gather historical values from stats_db
    historical = []
    for day_data in list(stats_db.values())[-30:]:
        avg = day_data.get('temperature_avg', {}).get(entity_id)
        if avg:
            historical.append(avg)
    if len(historical) < 7:
        return None
    mean = sum(historical) / len(historical)
    std_dev = (sum((x - mean) ** 2 for x in historical) / len(historical)) ** 0.5
    if std_dev == 0:
        return None
    z_score = abs(value - mean) / std_dev
    if z_score > 2.5:  # More than 2.5 std devs from mean = anomaly
        return {
            'entity': entity_id,
            'label': label,
            'value': value,
            'historical_mean': round(mean, 1),
            'z_score': round(z_score, 2),
            'time': datetime.now().isoformat(),
        }
    return None


def handle_event(ev):
    """v2.0: React to Event Bus events — accumulate stats."""
    global departure_count, arrival_count
    eid = ev.get('entity_id', '')
    new = ev.get('new_state', '')
    old = ev.get('old_state', '')
    sig = ev.get('significant', False)

    action = None

    # Presence tracking
    if 'iphone_presence' in eid:
        if new == 'off' and old == 'on':
            departure_count += 1
            action = 'departure_counted'
        elif new == 'on' and old == 'off':
            arrival_count += 1
            action = 'arrival_counted'

    # Motion counting by room
    elif 'motion' in eid and new == 'on':
        motion_counts[eid] += 1
        action = 'motion_counted'

    # Temperature tracking
    elif 'temperature' in eid:
        try:
            val = float(new)
            temp_readings[eid].append(val)
            # Keep last 288 readings per entity (1 per 5min = 24h)
            temp_readings[eid] = temp_readings[eid][-288:]
            # Check for anomaly
            anomaly = detect_anomaly(eid, val, eid)
            if anomaly:
                logger.warning(f'ANOMALY: {anomaly}')
                event_actions.append({'time': datetime.now().isoformat(), 'event': eid, 'action': 'anomaly_detected', 'detail': anomaly})
                return
            action = 'temp_tracked'
        except (ValueError, TypeError):
            pass

    # Vacuum session end — record to today's stats
    elif 'vacuum' in eid and new in ['docked', 'standby'] and old == 'cleaning':
        today = datetime.now().strftime('%Y-%m-%d')
        if today not in stats_db:
            stats_db[today] = {}
        stats_db[today].setdefault('vacuum_sessions', 0)
        stats_db[today]['vacuum_sessions'] += 1
        save_stats_db()
        action = 'vacuum_session_recorded'

    # Weather change
    elif 'weather' in eid and sig:
        today = datetime.now().strftime('%Y-%m-%d')
        if today not in stats_db:
            stats_db[today] = {}
        stats_db[today].setdefault('weather_states', [])
        stats_db[today]['weather_states'].append({'time': datetime.now().isoformat(), 'state': new})
        save_stats_db()
        action = 'weather_recorded'

    if action and action not in ('temp_tracked', 'motion_counted'):
        event_actions.append({
            'time': datetime.now().isoformat(),
            'event': eid,
            'action': action,
            'old': old,
            'new': new,
        })


def event_bus_subscriber():
    """v2.0: SSE subscriber thread."""
    while True:
        try:
            logger.info(f'Connecting to Event Bus SSE: {EVENT_BUS_URL}/events/stream')
            response = http.get(f'{EVENT_BUS_URL}/events/stream', stream=True, timeout=None)
            client = sseclient.SSEClient(response)
            logger.info('Event Bus SSE connected')
            for event in client.events():
                try:
                    ev = json.loads(event.data)
                    handle_event(ev)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.error(f'Event handling error: {e}')
        except Exception as e:
            logger.error(f'Event Bus SSE disconnected: {e}')
        logger.info('Reconnecting to Event Bus in 10s...')
        time.sleep(10)


def daily_flush_loop():
    """Flush today's stats to disk every 15 minutes, and at midnight roll over."""
    last_day = datetime.now().strftime('%Y-%m-%d')
    while True:
        time.sleep(900)  # 15 minutes
        flush_today()
        today = datetime.now().strftime('%Y-%m-%d')
        if today != last_day:
            logger.info(f'Day rollover: {last_day} -> {today}')
            last_day = today


# ---- Existing HA query helpers (unchanged from v1) ----

def ha_get(path):
    try:
        r = http.get(f'{HA_URL}/api{path}', headers={'Authorization': f'Bearer {HA_TOKEN}'}, timeout=15)
        return r.json() if r.status_code == 200 else None
    except:
        return None


def ha_history(entity_id, hours=24):
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    data = ha_get(f'/history/period/{start}?filter_entity_id={entity_id}&minimal_response&no_attributes')
    if data and data[0]: return data[0]
    return []


# ---- REST endpoints ----

@app.route('/')
def index():
    return jsonify({'name': 'Home Analytics Dashboard', 'version': '2.0.0', 'days_tracked': len(stats_db), 'event_bus': 'connected' if event_actions else 'waiting'})


@app.route('/health')
def health():
    states = ha_get('/states')
    return jsonify({'status': 'ok' if states else 'ha_unreachable', 'entities': len(states) if states else 0, 'days_tracked': len(stats_db), 'event_bus': 'connected' if event_actions else 'waiting'})


@app.route('/energy/today')
def energy_today():
    heater_kwh = ha_get('/states/input_number.bathroom_heater_daily_energy_kwh')
    heater_power = ha_get('/states/sensor.refrigerator_power')
    return jsonify({
        'heater_daily_kwh': float(heater_kwh['state']) if heater_kwh else 0,
        'heater_current_watts': float(heater_power['state']) if heater_power else 0,
        'estimated_cost_usd': round(float(heater_kwh['state']) * 0.35, 2) if heater_kwh else 0,
    })


@app.route('/energy/week')
def energy_week():
    heater = ha_get('/states/input_number.bathroom_heater_daily_energy_kwh')
    daily = float(heater['state']) if heater else 0
    return jsonify({
        'heater_estimated_weekly_kwh': round(daily * 7, 1),
        'heater_estimated_weekly_cost': round(daily * 7 * 0.35, 2),
        'rate_per_kwh': 0.35,
    })


@app.route('/climate/now')
def climate_now():
    rooms = {}
    sensors = {
        'Bedroom': {'temp': 'sensor.bedroom_co2_monitor_temperature', 'humidity': 'sensor.bedroom_co2_monitor_humidity', 'co2': 'sensor.bedroom_co2_monitor_carbon_dioxide'},
        'Living Room': {'temp': 'sensor.living_room_co2_monitor_temperature', 'humidity': 'sensor.living_room_co2_monitor_humidity', 'co2': 'sensor.living_room_co2_monitor_carbon_dioxide'},
        'Bathroom': {'temp': 'sensor.pr_humidity_sensor_temperature', 'humidity': 'sensor.bathroom_temp_humidity_sensor_humidity'},
        'Outdoor': {'temp': 'sensor.outdoor_humidity_sensor_temperature', 'humidity': 'sensor.outdoor_humidity_sensor_humidity'},
    }
    for room, ents in sensors.items():
        rooms[room] = {}
        for key, eid in ents.items():
            s = ha_get(f'/states/{eid}')
            rooms[room][key] = float(s['state']) if s and s['state'] not in ['unavailable', 'unknown'] else None
    return jsonify(rooms)


@app.route('/climate/history/<int:hours>')
def climate_history(hours):
    results = {}
    for eid, label in [
        ('sensor.bedroom_co2_monitor_temperature', 'bedroom_temp'),
        ('sensor.bedroom_co2_monitor_carbon_dioxide', 'bedroom_co2'),
        ('sensor.living_room_co2_monitor_temperature', 'lr_temp')
    ]:
        h = ha_history(eid, hours)
        vals = []
        for e in h:
            try: vals.append({'time': e['last_changed'], 'value': float(e['state'])})
            except: pass
        results[label] = {'count': len(vals), 'min': min(v['value'] for v in vals) if vals else None, 'max': max(v['value'] for v in vals) if vals else None, 'avg': round(sum(v['value'] for v in vals) / len(vals), 1) if vals else None}
    return jsonify(results)


@app.route('/sleep/last-night')
def sleep_last():
    results = {}
    for eid, label in [
        ('sensor.bedroom_co2_monitor_temperature', 'temp'),
        ('sensor.bedroom_co2_monitor_humidity', 'humidity'),
        ('sensor.bedroom_co2_monitor_carbon_dioxide', 'co2')
    ]:
        h = ha_history(eid, 24)
        night_vals = []
        for e in h:
            try:
                t = datetime.fromisoformat(e['last_changed'].replace('Z', '+00:00'))
                if t.hour >= 22 or t.hour < 7:
                    night_vals.append(float(e['state']))
            except: pass
        if night_vals:
            results[label] = {'avg': round(sum(night_vals) / len(night_vals), 1), 'min': round(min(night_vals), 1), 'max': round(max(night_vals), 1), 'readings': len(night_vals)}
    score = 100
    if results.get('co2', {}).get('avg', 0) > 1000: score -= 30
    elif results.get('co2', {}).get('avg', 0) > 700: score -= 10
    temp = results.get('temp', {}).get('avg', 68)
    if temp and (temp < 63 or temp > 72): score -= 20
    elif temp and (temp < 65 or temp > 70): score -= 10
    hum = results.get('humidity', {}).get('avg', 45)
    if hum and (hum < 30 or hum > 60): score -= 15
    results['score'] = max(0, score)
    results['grade'] = 'A' if score >= 90 else 'B' if score >= 75 else 'C' if score >= 60 else 'D'
    return jsonify(results)


@app.route('/sleep/week')
def sleep_week():
    h = ha_history('sensor.bedroom_co2_monitor_carbon_dioxide', 168)
    nights = {}
    for e in h:
        try:
            t = datetime.fromisoformat(e['last_changed'].replace('Z', '+00:00'))
            if t.hour >= 22 or t.hour < 7:
                day = t.strftime('%A')
                if day not in nights: nights[day] = []
                nights[day].append(float(e['state']))
        except: pass
    return jsonify({day: {'avg_co2': round(sum(vals) / len(vals), 0), 'max_co2': round(max(vals), 0), 'readings': len(vals)} for day, vals in nights.items()})


@app.route('/presence/today')
def presence_today():
    h = ha_history('binary_sensor.iphone_presence', 24)
    events = []
    for e in h:
        events.append({'time': e['last_changed'], 'state': e['state']})
    home_mins = 0
    away_mins = 0
    for i, e in enumerate(events):
        if i < len(events) - 1:
            try:
                t1 = datetime.fromisoformat(e['last_changed'].replace('Z', '+00:00'))
                t2 = datetime.fromisoformat(events[i + 1]['last_changed'].replace('Z', '+00:00'))
                mins = (t2 - t1).total_seconds() / 60
                if e['state'] == 'on': home_mins += mins
                else: away_mins += mins
            except: pass
    return jsonify({'events': events, 'home_hours': round(home_mins / 60, 1), 'away_hours': round(away_mins / 60, 1), 'arrivals_today': arrival_count, 'departures_today': departure_count})


@app.route('/presence/patterns')
def presence_patterns():
    """v2.0: Historical presence patterns from stats DB."""
    patterns = []
    for day, data in sorted(stats_db.items())[-30:]:
        p = data.get('presence', {})
        if p:
            patterns.append({'date': day, 'arrivals': p.get('arrivals', 0), 'departures': p.get('departures', 0)})
    return jsonify({'days': patterns, 'total_days': len(patterns)})


@app.route('/batteries')
def batteries():
    states = ha_get('/states')
    if not states: return jsonify({'error': 'HA unreachable'}), 500
    batts = []
    for s in states:
        if 'battery' in s['entity_id'] and s['entity_id'].startswith('sensor.') and s['state'] not in ['unavailable', 'unknown']:
            try:
                val = float(s['state'])
                if 0 <= val <= 100:
                    batts.append({'entity': s['entity_id'], 'name': s['attributes'].get('friendly_name', s['entity_id']), 'level': val})
            except: pass
    batts.sort(key=lambda x: x['level'])
    return jsonify({'total': len(batts), 'low': [b for b in batts if b['level'] < 20], 'all': batts})


@app.route('/devices/health')
def device_health():
    states = ha_get('/states')
    if not states: return jsonify({'error': 'HA unreachable'}), 500
    unavailable = [s['attributes'].get('friendly_name', s['entity_id']) for s in states if s['state'] == 'unavailable' and not any(skip in s['entity_id'] for skip in ['bks_macbook', 'clawdbot', 'dryer', 'unnamed', 'playroom_sonos'])]
    automations = [s for s in states if s['entity_id'].startswith('automation.') and s['state'] == 'on']
    stale = [s['attributes'].get('friendly_name') for s in automations if s['attributes'].get('last_triggered') is None]
    return jsonify({'unavailable_count': len(unavailable), 'unavailable': unavailable[:20], 'stale_automations': stale, 'total_automations': len(automations)})


@app.route('/summary')
def summary():
    climate = ha_get('/states/sensor.bedroom_co2_monitor_temperature')
    presence = ha_get('/states/binary_sensor.iphone_presence')
    lock = ha_get('/states/lock.front_door_lock')
    vacuum = ha_get('/states/vacuum.robovac')
    soil = ha_get('/states/sensor.third_reality_soil_sensor_soil_moisture')
    return jsonify({
        'home': presence['state'] == 'on' if presence else None,
        'lock': lock['state'] if lock else None,
        'bedroom_temp': float(climate['state']) if climate else None,
        'vacuum': vacuum['state'] if vacuum else None,
        'soil_moisture': float(soil['state']) if soil else None,
        'days_tracked': len(stats_db),
        'arrivals_today': arrival_count,
        'departures_today': departure_count,
        'timestamp': datetime.now().isoformat(),
    })


# v2.0 endpoints

@app.route('/stats/daily')
def stats_daily():
    days = request.args.get('days', 30, type=int)
    recent = sorted(stats_db.items())[-days:]
    return jsonify({'days': [v for _, v in recent], 'total': len(recent)})


@app.route('/stats/monthly')
def stats_monthly():
    monthly = defaultdict(lambda: {'arrivals': 0, 'departures': 0, 'vacuum_sessions': 0, 'days': 0})
    for day, data in stats_db.items():
        month = day[:7]  # YYYY-MM
        m = monthly[month]
        m['days'] += 1
        p = data.get('presence', {})
        m['arrivals'] += p.get('arrivals', 0)
        m['departures'] += p.get('departures', 0)
        m['vacuum_sessions'] += data.get('vacuum_sessions', 0)
    return jsonify({'months': [{'month': k, **v} for k, v in sorted(monthly.items())]})


@app.route('/stats/anomalies')
def stats_anomalies():
    anomalies = [e for e in event_actions if e.get('action') == 'anomaly_detected']
    return jsonify({'anomalies': anomalies[-20:], 'total': len(anomalies)})


@app.route('/event-log')
def event_log():
    return jsonify(list(event_actions)[-30:])


if __name__ == '__main__':
    logger.info(f'Home Analytics Dashboard v2.0.0 on :{API_PORT}')
    load_stats_db()
    # v2.0: Start Event Bus SSE subscriber
    threading.Thread(target=event_bus_subscriber, daemon=True).start()
    # v2.0: Start daily flush loop
    threading.Thread(target=daily_flush_loop, daemon=True).start()
    logger.info('Event Bus subscriber + daily flush loop started')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
