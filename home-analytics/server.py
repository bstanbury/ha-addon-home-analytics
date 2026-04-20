#!/usr/bin/env python3
"""Home Analytics Dashboard v1.0.0
Trend analysis from HA history data.

Endpoints:
  GET /health
  GET /energy/today         — Today's energy usage
  GET /energy/week          — Weekly energy summary
  GET /climate/now           — Current climate across rooms
  GET /climate/history/<hours> — Climate trends
  GET /sleep/last-night      — Sleep environment analysis
  GET /sleep/week            — Weekly sleep quality
  GET /presence/today        — Today's presence pattern
  GET /presence/patterns     — Departure/arrival patterns
  GET /devices/health        — Device reliability scores
  GET /batteries             — All battery levels
  GET /summary               — Full home summary
"""
import os,json,time,logging
from datetime import datetime,timedelta,timezone
from flask import Flask,jsonify,request
import requests as http

HA_URL=os.environ.get('HA_URL','http://localhost:8123')
HA_TOKEN=os.environ.get('HA_TOKEN','')
API_PORT=int(os.environ.get('API_PORT','8095'))

app=Flask(__name__)
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
logger=logging.getLogger('home-analytics')

def ha_get(path):
    try:
        r=http.get(f'{HA_URL}/api{path}',headers={'Authorization':f'Bearer {HA_TOKEN}'},timeout=15)
        return r.json() if r.status_code==200 else None
    except: return None

def ha_history(entity_id,hours=24):
    start=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
    data=ha_get(f'/history/period/{start}?filter_entity_id={entity_id}&minimal_response&no_attributes')
    if data and data[0]: return data[0]
    return []

def avg_in_range(entries,start_hour,end_hour):
    vals=[]
    for e in entries:
        try:
            t=datetime.fromisoformat(e['last_changed'].replace('Z','+00:00'))
            h=t.hour
            if start_hour<=h or h<end_hour:
                v=float(e['state'])
                vals.append(v)
        except: pass
    return round(sum(vals)/len(vals),1) if vals else None

@app.route('/')
def index():
    return jsonify({'name':'Home Analytics Dashboard','version':'1.0.0'})

@app.route('/health')
def health():
    states=ha_get('/states')
    return jsonify({'status':'ok' if states else 'ha_unreachable','entities':len(states) if states else 0})

@app.route('/energy/today')
def energy_today():
    heater_kwh=ha_get('/states/input_number.bathroom_heater_daily_energy_kwh')
    heater_power=ha_get('/states/sensor.refrigerator_power')
    return jsonify({
        'heater_daily_kwh':float(heater_kwh['state']) if heater_kwh else 0,
        'heater_current_watts':float(heater_power['state']) if heater_power else 0,
        'estimated_cost_usd':round(float(heater_kwh['state'])*0.35,2) if heater_kwh else 0,
    })

@app.route('/energy/week')
def energy_week():
    # Estimate from daily average
    heater=ha_get('/states/input_number.bathroom_heater_daily_energy_kwh')
    daily=float(heater['state']) if heater else 0
    return jsonify({
        'heater_estimated_weekly_kwh':round(daily*7,1),
        'heater_estimated_weekly_cost':round(daily*7*0.35,2),
        'rate_per_kwh':0.35,
    })

@app.route('/climate/now')
def climate_now():
    rooms={}
    sensors={
        'Bedroom':{'temp':'sensor.bedroom_co2_monitor_temperature','humidity':'sensor.bedroom_co2_monitor_humidity','co2':'sensor.bedroom_co2_monitor_carbon_dioxide'},
        'Living Room':{'temp':'sensor.living_room_co2_monitor_temperature','humidity':'sensor.living_room_co2_monitor_humidity','co2':'sensor.living_room_co2_monitor_carbon_dioxide'},
        'Bathroom':{'temp':'sensor.pr_humidity_sensor_temperature','humidity':'sensor.bathroom_temp_humidity_sensor_humidity'},
        'Outdoor':{'temp':'sensor.outdoor_humidity_sensor_temperature','humidity':'sensor.outdoor_humidity_sensor_humidity'},
    }
    for room,ents in sensors.items():
        rooms[room]={}
        for key,eid in ents.items():
            s=ha_get(f'/states/{eid}')
            rooms[room][key]=float(s['state']) if s and s['state'] not in ['unavailable','unknown'] else None
    return jsonify(rooms)

@app.route('/climate/history/<int:hours>')
def climate_history(hours):
    results={}
    for eid,label in [('sensor.bedroom_co2_monitor_temperature','bedroom_temp'),('sensor.bedroom_co2_monitor_carbon_dioxide','bedroom_co2'),('sensor.living_room_co2_monitor_temperature','lr_temp')]:
        h=ha_history(eid,hours)
        vals=[]
        for e in h:
            try: vals.append({'time':e['last_changed'],'value':float(e['state'])})
            except: pass
        results[label]={'count':len(vals),'min':min(v['value'] for v in vals) if vals else None,'max':max(v['value'] for v in vals) if vals else None,'avg':round(sum(v['value'] for v in vals)/len(vals),1) if vals else None}
    return jsonify(results)

@app.route('/sleep/last-night')
def sleep_last():
    # Analyze 10pm-7am window
    results={}
    for eid,label in [('sensor.bedroom_co2_monitor_temperature','temp'),('sensor.bedroom_co2_monitor_humidity','humidity'),('sensor.bedroom_co2_monitor_carbon_dioxide','co2')]:
        h=ha_history(eid,24)
        night_vals=[]
        for e in h:
            try:
                t=datetime.fromisoformat(e['last_changed'].replace('Z','+00:00'))
                hour=t.hour
                if hour>=22 or hour<7:
                    night_vals.append(float(e['state']))
            except: pass
        if night_vals:
            results[label]={'avg':round(sum(night_vals)/len(night_vals),1),'min':round(min(night_vals),1),'max':round(max(night_vals),1),'readings':len(night_vals)}
    # Score
    score=100
    if results.get('co2',{}).get('avg',0)>1000: score-=30
    elif results.get('co2',{}).get('avg',0)>700: score-=10
    temp=results.get('temp',{}).get('avg',68)
    if temp and (temp<63 or temp>72): score-=20
    elif temp and (temp<65 or temp>70): score-=10
    hum=results.get('humidity',{}).get('avg',45)
    if hum and (hum<30 or hum>60): score-=15
    results['score']=max(0,score)
    results['grade']='A' if score>=90 else 'B' if score>=75 else 'C' if score>=60 else 'D'
    return jsonify(results)

@app.route('/sleep/week')
def sleep_week():
    h=ha_history('sensor.bedroom_co2_monitor_carbon_dioxide',168)
    # Group by night
    nights={}
    for e in h:
        try:
            t=datetime.fromisoformat(e['last_changed'].replace('Z','+00:00'))
            if t.hour>=22 or t.hour<7:
                day=t.strftime('%A')
                if day not in nights: nights[day]=[]
                nights[day].append(float(e['state']))
        except: pass
    results={}
    for day,vals in nights.items():
        results[day]={'avg_co2':round(sum(vals)/len(vals),0),'max_co2':round(max(vals),0),'readings':len(vals)}
    return jsonify(results)

@app.route('/presence/today')
def presence_today():
    h=ha_history('binary_sensor.iphone_presence',24)
    events=[]
    for e in h:
        events.append({'time':e['last_changed'],'state':e['state']})
    home_mins=0
    away_mins=0
    for i,e in enumerate(events):
        if i<len(events)-1:
            try:
                t1=datetime.fromisoformat(e['last_changed'].replace('Z','+00:00'))
                t2=datetime.fromisoformat(events[i+1]['last_changed'].replace('Z','+00:00'))
                mins=(t2-t1).total_seconds()/60
                if e['state']=='on': home_mins+=mins
                else: away_mins+=mins
            except: pass
    return jsonify({'events':events,'home_hours':round(home_mins/60,1),'away_hours':round(away_mins/60,1)})

@app.route('/batteries')
def batteries():
    states=ha_get('/states')
    if not states: return jsonify({'error':'HA unreachable'}),500
    batts=[]
    for s in states:
        if 'battery' in s['entity_id'] and s['entity_id'].startswith('sensor.') and s['state'] not in ['unavailable','unknown']:
            try:
                val=float(s['state'])
                if 0<=val<=100:
                    batts.append({'entity':s['entity_id'],'name':s['attributes'].get('friendly_name',s['entity_id']),'level':val})
            except: pass
    batts.sort(key=lambda x:x['level'])
    return jsonify({'total':len(batts),'low':[b for b in batts if b['level']<20],'all':batts})

@app.route('/devices/health')
def device_health():
    states=ha_get('/states')
    if not states: return jsonify({'error':'HA unreachable'}),500
    unavailable=[s['attributes'].get('friendly_name',s['entity_id']) for s in states if s['state']=='unavailable' and not any(skip in s['entity_id'] for skip in ['bks_macbook','clawdbot','dryer','unnamed','playroom_sonos'])]
    automations=[s for s in states if s['entity_id'].startswith('automation.') and s['state']=='on']
    stale=[s['attributes'].get('friendly_name') for s in automations if s['attributes'].get('last_triggered') is None]
    return jsonify({'unavailable_count':len(unavailable),'unavailable':unavailable[:20],'stale_automations':stale,'total_automations':len(automations)})

@app.route('/summary')
def summary():
    climate=ha_get('/states/sensor.bedroom_co2_monitor_temperature')
    presence=ha_get('/states/binary_sensor.iphone_presence')
    lock=ha_get('/states/lock.front_door_lock')
    vacuum=ha_get('/states/vacuum.robovac')
    soil=ha_get('/states/sensor.third_reality_soil_sensor_soil_moisture')
    return jsonify({
        'home':presence['state']=='on' if presence else None,
        'lock':lock['state'] if lock else None,
        'bedroom_temp':float(climate['state']) if climate else None,
        'vacuum':vacuum['state'] if vacuum else None,
        'soil_moisture':float(soil['state']) if soil else None,
        'timestamp':datetime.now().isoformat(),
    })

if __name__=='__main__':
    logger.info(f'Home Analytics Dashboard v1.0.0 on :{API_PORT}')
    app.run(host='0.0.0.0',port=API_PORT,debug=False)
