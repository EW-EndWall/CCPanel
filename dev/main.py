from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import obd
import time
import threading
import json
import os
from datetime import datetime, timedelta
import random

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# OBD bağlantısı
connection = None
try:
    connection = obd.OBD()  # OBD bağlantısını aç (otomatik port algılar)
    if connection.is_connected():
        print("OBD bağlantısı başarılı")
    else:
        print("OBD bağlantısı kurulamadı, simülasyon moduna geçiliyor")
        connection = None
except:
    print("OBD kütüphanesi veya bağlantı hatası, simülasyon moduna geçiliyor")
    connection = None

# Sensör komutları
sensor_commands = {
    "rpm": obd.commands.RPM,
    "speed": obd.commands.SPEED,
    "motor_load": obd.commands.ENGINE_LOAD,
    "motor_temp": obd.commands.COOLANT_TEMP,
    "oil_temp": obd.commands.OIL_TEMP,
    "intake_pressure": obd.commands.INTAKE_PRESSURE,
    "fuel_level": obd.commands.FUEL_LEVEL,
    "battery_voltage": obd.commands.CONTROL_MODULE_VOLTAGE
}

# Global state
sensor_data = {
    "turbo_pressure": 1.2,
    "air_fuel_ratio": 14.7,
    "rpm": 0,
    "speed": 0,
    "fuel_consumption": 0,
    "motor_load": 0,
    "motor_temp": 0,
    "oil_temp": 0,
    "battery_voltage": 0
}

error_codes = []

control_states = {
    "light": True,
    "seat_heating": False,
    "seat_cooling": False,
    "steering_heating": False,
    "spotlight": False,
    "screen_switch": False
}

radio_data = {
    "stations": [
        {"name": "Power FM 98.5", "freq": "98.5"},
        {"name": "Best FM 95.7", "freq": "95.7"},
        {"name": "Number 1 FM 92.0", "freq": "92.0"},
        {"name": "Radyo D 90.8", "freq": "90.8"},
        {"name": "Joy FM 94.3", "freq": "94.3"}
    ],
    "current_station": 0,
    "is_playing": True,
    "volume": 70
}

# Log directory setup
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

current_log_file = None
last_hour = None

def get_log_filename():
    now = datetime.now()
    return f"{LOG_DIR}/sensor_data_{now.strftime('%Y-%m-%d_%H')}.json"

def rotate_logs():
    global current_log_file, last_hour
    
    now = datetime.now()
    current_hour = now.strftime('%Y-%m-%d_%H')
    
    if current_hour != last_hour:
        if current_log_file:
            current_log_file.close()
        
        # Create new log file
        current_log_file = open(get_log_filename(), "a")
        last_hour = current_hour
        
        # Delete logs older than 2 hours
        two_hours_ago = now - timedelta(hours=2)
        old_file = f"{LOG_DIR}/sensor_data_{two_hours_ago.strftime('%Y-%m-%d_%H')}.json"
        if os.path.exists(old_file):
            os.remove(old_file)

def log_sensor_data():
    rotate_logs()
    
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "sensors": sensor_data.copy()
    }
    
    if current_log_file:
        current_log_file.write(json.dumps(log_entry) + "\n")
        current_log_file.flush()

def get_obd_data():
    """Get real sensor data from OBD"""
    if connection is None or not connection.is_connected():
        # Fallback to simulation if no connection
        simulate_obd_data()
        return
    
    # RPM
    cmd = obd.commands.RPM
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["rpm"] = 0
    else:
        sensor_data["rpm"] = int(response.value.magnitude)
    
    # Speed
    cmd = obd.commands.SPEED
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["speed"] = 0
    else:
        sensor_data["speed"] = int(response.value.magnitude)
    
    # Engine Load
    cmd = obd.commands.ENGINE_LOAD
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["motor_load"] = 0
    else:
        sensor_data["motor_load"] = int(response.value.magnitude)
    
    # Coolant Temp
    cmd = obd.commands.COOLANT_TEMP
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["motor_temp"] = 0
    else:
        sensor_data["motor_temp"] = int(response.value.magnitude)
    
    # Oil Temp (some cars may not support this)
    cmd = obd.commands.OIL_TEMP
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["oil_temp"] = 0
    else:
        sensor_data["oil_temp"] = int(response.value.magnitude)
    
    # Intake Pressure (used for turbo pressure)
    cmd = obd.commands.INTAKE_PRESSURE
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["turbo_pressure"] = 0
    else:
        # Convert kPa to bar (1 bar = 100 kPa)
        sensor_data["turbo_pressure"] = round(response.value.magnitude / 100, 1)
    
    # Fuel Level
    cmd = obd.commands.FUEL_LEVEL
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["fuel_consumption"] = 0
    else:
        # Convert percentage to estimated L/100km (rough estimation)
        fuel_percent = response.value.magnitude
        if fuel_percent > 0:
            # This is a rough estimation, real calculation would need more data
            sensor_data["fuel_consumption"] = round(10 - (fuel_percent / 10), 1)
        else:
            sensor_data["fuel_consumption"] = 0
    
    # Battery Voltage
    cmd = obd.commands.CONTROL_MODULE_VOLTAGE
    response = connection.query(cmd)
    if response.is_null():
        sensor_data["battery_voltage"] = 0
    else:
        sensor_data["battery_voltage"] = round(response.value.magnitude, 1)
    
    # Air Fuel Ratio (if supported)
    try:
        cmd = obd.commands.AIR_FUEL_RATIO
        response = connection.query(cmd)
        if response.is_null():
            sensor_data["air_fuel_ratio"] = 14.7  # Stoichiometric ratio
        else:
            sensor_data["air_fuel_ratio"] = round(response.value.magnitude, 1)
    except:
        sensor_data["air_fuel_ratio"] = 14.7

def get_dtc_codes():
    """Get Diagnostic Trouble Codes from OBD"""
    global error_codes
    
    if connection is None or not connection.is_connected():
        # Fallback to demo errors if no connection
        error_codes = [
            {"code": "P0300", "title": "Rastgele Silindir Ateşleme Hatası", "description": "Motorun silindirlerinden birinde veya birkaçında ateşleme hatası tespit edildi.", "time": datetime.now().strftime("%d.%m.%Y %H:%M")},
            {"code": "P0171", "title": "Sistem Zayıf (Bank 1)", "description": "Motorun yakıt sistemi çok fakir bir karışım sağlıyor.", "time": datetime.now().strftime("%d.%m.%Y %H:%M")},
            {"code": "P0420", "title": "Katalitik Konvertör Verimliliği", "description": "Katalitik konvertörün verimliliği eşik değerin altında.", "time": datetime.now().strftime("%d.%m.%Y %H:%M")},
            {"code": "P0455", "title": "Emme Kontrol Sistemi Büyük Sızıntı", "description": "Yakıt buharı emme kontrol sisteminde büyük bir sızıntı tespit edildi.", "time": datetime.now().strftime("%d.%m.%Y %H:%M")}
        ]
        return
    
    # Get DTC codes
    cmd = obd.commands.GET_DTC
    response = connection.query(cmd)
    
    if response.is_null():
        error_codes = []
    else:
        error_codes = []
        dtc_list = response.value
        
        # DTC descriptions mapping
        dtc_descriptions = {
            "P0300": {"title": "Rastgele Silindir Ateşleme Hatası", "description": "Motorun silindirlerinden birinde veya birkaçında ateşleme hatası tespit edildi."},
            "P0171": {"title": "Sistem Zayıf (Bank 1)", "description": "Motorun yakıt sistemi çok fakir bir karışım sağlıyor."},
            "P0420": {"title": "Katalitik Konvertör Verimliliği", "description": "Katalitik konvertörün verimliliği eşik değerin altında."},
            "P0455": {"title": "Emme Kontrol Sistemi Büyük Sızıntı", "description": "Yakıt buharı emme kontrol sisteminde büyük bir sızıntı tespit edildi."},
            "P0101": {"title": "MAF Sensörü Devre Aralığı/Performans", "description": "Kütle hava akış sensöründe aralık veya performans sorunu."},
            "P0135": {"title": "O2 Sensörü Isıtıcı Devresi (Bank 1, Sensör 1)", "description": "Oksijen sensörü ısıtıcı devresinde arıza."},
            "P0301": {"title": "Silindir 1 Ateşleme Hatası", "description": "Silindir 1'de ateşleme hatası tespit edildi."},
            "P0442": {"title": "Emme Kontrol Sistemi Küçük Sızıntı", "description": "Yakıt buharı emme kontrol sisteminde küçük bir sızıntı tespit edildi."},
            "P0500": {"title": "Hız Sensörü", "description": "Araç hızı sensöründe arıza."}
        }
        
        for dtc in dtc_list:
            code = str(dtc)
            description = dtc_descriptions.get(code, {
                "title": f"Hata Kodu {code}",
                "description": "Tanımlanmamış hata kodu."
            })
            
            error_codes.append({
                "code": code,
                "title": description["title"],
                "description": description["description"],
                "time": datetime.now().strftime("%d.%m.%Y %H:%M")
            })

def simulate_obd_data():
    """Simulate OBD data when real connection is not available"""
    # Simulate realistic sensor values with some randomness
    sensor_data["turbo_pressure"] = round(0.8 + random.random() * 0.8, 1)
    sensor_data["air_fuel_ratio"] = round(14.0 + random.random() * 1.5, 1)
    sensor_data["rpm"] = int(1500 + random.random() * 2500)
    sensor_data["speed"] = int(60 + random.random() * 80)
    sensor_data["fuel_consumption"] = round(6.0 + random.random() * 3.0, 1)
    sensor_data["motor_load"] = int(40 + random.random() * 40)
    sensor_data["motor_temp"] = int(85 + random.random() * 25)
    sensor_data["oil_temp"] = int(80 + random.random() * 20)
    sensor_data["battery_voltage"] = round(12.0 + random.random() * 0.8, 1)

def update_sensors():
    """Update sensor data every 500ms and send via WebSocket"""
    while True:
        get_obd_data()
        log_sensor_data()
        
        # Send sensor data via WebSocket
        socketio.emit('sensor_update', sensor_data)
        
        # Check for critical values and send alerts
        if sensor_data["motor_temp"] > 100:
            socketio.emit('alert', {
                'type': 'warning',
                'message': 'Motor sıcaklığı kritik seviyede!',
                'value': sensor_data["motor_temp"]
            })
        
        if sensor_data["battery_voltage"] < 12.0:
            socketio.emit('alert', {
                'type': 'warning',
                'message': 'Akü voltajı düşük!',
                'value': sensor_data["battery_voltage"]
            })
            
        time.sleep(0.5)

def update_errors():
    """Update error codes every 15 minutes and send via WebSocket"""
    while True:
        get_dtc_codes()
        socketio.emit('error_update', error_codes)
        time.sleep(900)  # 15 minutes

# API Endpoints
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/sensors', methods=['GET'])
def get_sensors():
    return jsonify(sensor_data)

@app.route('/api/errors', methods=['GET'])
def get_errors():
    return jsonify(error_codes)

@app.route('/api/controls', methods=['GET'])
def get_controls():
    return jsonify(control_states)

@app.route('/api/control/<control_name>', methods=['POST'])
def set_control(control_name):
    if control_name in control_states:
        data = request.get_json()
        if 'state' in data:
            control_states[control_name] = data['state']
            # Send control update via WebSocket
            socketio.emit('control_update', {
                'control': control_name,
                'state': control_states[control_name]
            })
            return jsonify({"success": True, "control": control_name, "state": control_states[control_name]})
    return jsonify({"success": False, "error": "Invalid control"}), 400

@app.route('/api/radio', methods=['GET'])
def get_radio():
    return jsonify(radio_data)

@app.route('/api/radio/station', methods=['POST'])
def set_radio_station():
    data = request.get_json()
    if 'station' in data and 0 <= data['station'] < len(radio_data['stations']):
        radio_data['current_station'] = data['station']
        # Send radio update via WebSocket
        socketio.emit('radio_update', radio_data)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid station"}), 400

@app.route('/api/radio/play', methods=['POST'])
def set_radio_play():
    data = request.get_json()
    if 'playing' in data:
        radio_data['is_playing'] = data['playing']
        # Send radio update via WebSocket
        socketio.emit('radio_update', radio_data)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Missing parameter"}), 400

@app.route('/api/radio/volume', methods=['POST'])
def set_radio_volume():
    data = request.get_json()
    if 'volume' in data and 0 <= data['volume'] <= 100:
        radio_data['volume'] = data['volume']
        # Send radio update via WebSocket
        socketio.emit('radio_update', radio_data)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid volume"}), 400

@app.route('/api/obd/status', methods=['GET'])
def get_obd_status():
    return jsonify({
        "connected": connection is not None and connection.is_connected(),
        "port": connection.port() if connection and connection.is_connected() else None
    })

# WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    print('Client connected')
    # Send initial data to the newly connected client
    emit('sensor_update', sensor_data)
    emit('error_update', error_codes)
    
    # Send each control state individually
    for control_name, state in control_states.items():
        emit('control_update', {
            'control': control_name,
            'state': state
        })
    
    emit('radio_update', radio_data)

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('control_change')
def handle_control_change(data):
    control_name = data.get('control')
    state = data.get('state')
    
    if control_name in control_states:
        control_states[control_name] = state
        # Broadcast the control change to all clients
        emit('control_update', {
            'control': control_name,
            'state': state
        }, broadcast=True)

@socketio.on('radio_change')
def handle_radio_change(data):
    if 'station' in data and 0 <= data['station'] < len(radio_data['stations']):
        radio_data['current_station'] = data['station']
        # Broadcast the radio change to all clients
        emit('radio_update', radio_data, broadcast=True)
    
    if 'playing' in data:
        radio_data['is_playing'] = data['playing']
        # Broadcast the radio change to all clients
        emit('radio_update', radio_data, broadcast=True)
    
    if 'volume' in data and 0 <= data['volume'] <= 100:
        radio_data['volume'] = data['volume']
        # Broadcast the radio change to all clients
        emit('radio_update', radio_data, broadcast=True)

if __name__ == '__main__':
    # Start background threads
    sensor_thread = threading.Thread(target=update_sensors, daemon=True)
    error_thread = threading.Thread(target=update_errors, daemon=True)
    
    sensor_thread.start()
    error_thread.start()
    
    # Initialize log file
    rotate_logs()
    
    # Initial data fetch
    get_obd_data()
    get_dtc_codes()
    
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)