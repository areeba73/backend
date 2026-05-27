from flask import Blueprint, request, jsonify
from flask_cors import cross_origin
import requests
import os
import logging
from dotenv import load_dotenv
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY")
FRONTEND_RESET_URL = os.getenv("FRONTEND_RESET_URL", "http://localhost:5173/reset")

def validate_email(email):
    """Validate email format"""
    import re
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_password(password):
    """Validate password strength"""
    if len(password) < 6:
        return False, "Password must be at least 6 characters"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, "Password is valid"

def handle_firebase_error(firebase_error_msg):
    """Map Firebase error messages"""
    error_map = {
        'EMAIL_NOT_FOUND': 'No account found with this email address.',
        'INVALID_RESET_CODE': 'This password reset link is invalid or has expired.',
        'EXPIRED_OOB_CODE': 'This password reset link has expired. Please request a new one.',
        'USER_DISABLED': 'This account has been disabled.',
        'TOO_MANY_ATTEMPTS_TRY_LATER': 'Too many failed attempts. Please try again later.',
    }
    
    for key, value in error_map.items():
        if key in firebase_error_msg:
            return value
    
    return firebase_error_msg

# ===== FORGOT PASSWORD =====
@auth_bp.route('/forgot-password', methods=['POST', 'OPTIONS'])
@cross_origin()
def forgot_password():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.json
        email = data.get('email', '').strip()

        if not email:
            return jsonify({"error": "Email is required"}), 400
        
        if not validate_email(email):
            return jsonify({"error": "Invalid email format"}), 400

        logger.info(f"Forgot password request for: {email}")

        url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_WEB_API_KEY}"
        
        payload = {
            "requestType": "PASSWORD_RESET",
            "email": email,
            "continueUrl": FRONTEND_RESET_URL,
            "canHandleCodeInApp": True
        }

        response = requests.post(url, json=payload, timeout=10)
        res_data = response.json()

        if response.status_code == 200:
            logger.info(f"Reset email sent to: {email}")
            return jsonify({
                "message": "A password reset link has been sent to your email.",
                "email": email,
                "timestamp": datetime.now().isoformat()
            }), 200
        else:
            error_msg = res_data.get('error', {}).get('message', 'Failed to send email')
            user_friendly_msg = handle_firebase_error(error_msg)
            logger.warning(f"Firebase error: {error_msg}")
            return jsonify({"error": user_friendly_msg}), 400

    except Exception as e:
        logger.error(f"Error in forgot_password: {str(e)}")
        return jsonify({"error": "An error occurred. Please try again later."}), 500

# ===== COMPLETE RESET =====
@auth_bp.route('/complete-reset', methods=['POST', 'OPTIONS'])
@cross_origin()
def complete_reset():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.json
        oob_code = data.get('oobCode', '').strip()
        new_password = data.get('newPassword', '')

        if not oob_code or not new_password:
            return jsonify({"error": "Reset code and new password are required"}), 400
        
        is_valid, validation_msg = validate_password(new_password)
        if not is_valid:
            return jsonify({"error": validation_msg}), 400

        logger.info("Completing password reset")

        url = f"https://identitytoolkit.googleapis.com/v1/accounts:resetPassword?key={FIREBASE_WEB_API_KEY}"
        
        payload = {
            "oobCode": oob_code,
            "newPassword": new_password
        }

        response = requests.post(url, json=payload, timeout=10)
        res_data = response.json()

        if response.status_code == 200:
            logger.info("Password reset completed successfully")
            return jsonify({
                "message": "Your password has been successfully updated.",
                "email": res_data.get('email', ''),
                "timestamp": datetime.now().isoformat()
            }), 200
        else:
            error_msg = res_data.get('error', {}).get('message', 'Failed to reset password')
            user_friendly_msg = handle_firebase_error(error_msg)
            logger.warning(f"Password reset failed: {error_msg}")
            return jsonify({"error": user_friendly_msg}), 400

    except Exception as e:
        logger.error(f"Error in complete_reset: {str(e)}")
        return jsonify({"error": "An error occurred. Please try again later."}), 500