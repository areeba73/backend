from flask import Flask, request, make_response, jsonify
from flask_cors import CORS
from routes.userroutes import user_bp
from routes.doctorroutes import doctor_bp
from routes.adminroutes import admin_bp
from routes.emotionroutes import emotion_bp
from routes.forgetreset import auth_bp
import os
import logging

# ===== LOGGING SETUP =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ===== CORS CONFIGURATION =====
CORS(app, 
     origins=["http://localhost:5173", "http://localhost:3000", "http://localhost:5000"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization"],
     supports_credentials=True,
     max_age=3600)

# ===== FILE UPLOAD CONFIG =====
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

# ===== PREFLIGHT REQUEST HANDLER =====
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = make_response()
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.status_code = 200
        return response

# ===== REQUEST LOGGING =====
@app.after_request
def log_response(response):
    logger.info(f"{request.method} {request.path} - Status: {response.status_code}")
    return response

# ===== GLOBAL ERROR HANDLER =====
@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Route not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal Server Error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500

# ===== REGISTER BLUEPRINTS =====
app.register_blueprint(user_bp, url_prefix='/user')
app.register_blueprint(doctor_bp, url_prefix='/doctor')
app.register_blueprint(admin_bp, url_prefix='/admin')  
app.register_blueprint(auth_bp, url_prefix='/auth')
app.register_blueprint(emotion_bp, url_prefix='/api')

logger.info("All blueprints registered successfully!")

@app.route('/health', methods=['GET'])
def main_health():
    return jsonify({"status": "ok", "service": "EmoTrack API"}), 200

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}")
    app.run(debug=False, host='0.0.0.0', port=port)
