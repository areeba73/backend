from flask import Blueprint, request, jsonify
from firebase_admin import auth, firestore
import requests
from config import db, Config
from datetime import datetime, timedelta
import threading
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

doctor_bp = Blueprint('doctor_auth', __name__)

def delete_unverified_doctor_after_12h(uid, email):
    """Delete unverified doctor after 12 hours (43200 seconds)"""
    def delayed_delete():
        try:
            time.sleep(43200)  # 12 hours = 12 × 60 × 60 = 43200 seconds
            user = auth.get_user(uid)
            
            if not user.email_verified:
                auth.delete_user(uid)
                db.collection('users').document(uid).delete()
                logger.info(f" Unverified doctor {email} deleted after 12 hours")
        except Exception as e:
            logger.error(f" Error deleting unverified doctor {email}: {str(e)}")
    
    thread = threading.Thread(target=delayed_delete, daemon=True)
    thread.start()

@doctor_bp.route('/signup', methods=['POST', 'OPTIONS'])
def doctor_signup():
    if request.method == 'OPTIONS':
        return '', 204

    data = request.json
    
    full_name = data.get('fullName', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')
    speciality = data.get('speciality', '')
    clinic = data.get('clinicName', '')
    mobile = data.get('mobile', '')

    try:
        logger.info(f" Creating doctor account for: {email}")
        
        user = auth.create_user(email=email, password=password, display_name=full_name)
        
        is_admin = (email.lower() == Config.ADMIN_EMAIL.lower())
        role = 'admin' if is_admin else 'doctor'
        
        # Email Verification for non-admin doctors only
        if not is_admin:
            logger.info(f" Generating ID token for doctor: {email}")
            custom_token = auth.create_custom_token(user.uid).decode('utf-8')
            id_token_res = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={Config.FIREBASE_WEB_API_KEY}",
                json={"token": custom_token, "returnSecureToken": True},
                timeout=10
            )
            
            if id_token_res.status_code != 200:
                logger.error(f" Failed to get ID token: {id_token_res.json()}")
                return jsonify({"error": "Failed to generate token"}), 400
            
            id_token = id_token_res.json().get('idToken')
            
            # Send Verification Email
            verify_payload = {
                "requestType": "VERIFY_EMAIL",
                "idToken": id_token,
                "continueUrl": "http://localhost:5173/auth"
            }
            
            logger.info(f" Sending verification email to doctor: {email}")
            verify_response = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={Config.FIREBASE_WEB_API_KEY}",
                json=verify_payload,
                timeout=10
            )
            
            logger.info(f" Verification email response status: {verify_response.status_code}")
            
            if verify_response.status_code == 200:
                logger.info(f" Verification email sent successfully to {email}")
                delete_unverified_doctor_after_12h(user.uid, email)
            else:
                logger.error(f" Email send failed: {verify_response.json()}")
                auth.delete_user(user.uid)
                return jsonify({"error": "Failed to send verification email"}), 400
        else:
            logger.info(f" Admin doctor account created: {email}")
            auth.update_user(user.uid, email_verified=True)

        # Save Doctor with all details
        db.collection('users').document(user.uid).set({
            'fullName': full_name,
            'email': email,
            'role': role,
            'speciality': speciality,
            'clinicName': clinic,
            'mobile': mobile,
            'createdAt': firestore.SERVER_TIMESTAMP,
            'emailVerified': False if not is_admin else True,
            'verificationDeadline': datetime.utcnow() + timedelta(hours=12) if not is_admin else None
        })
        
        message = "Admin account created!" if is_admin else "Doctor registered! Check your email for verification link. You have 12 hours to verify."
        return jsonify({
            "message": message,
            "user": {
                "uid": user.uid,
                "email": email,
                "fullName": full_name,
                "role": role,
                "speciality": speciality,
                "clinicName": clinic,
                "mobile": mobile
            }
        }), 201
        
    except Exception as e:
        logger.error(f" Doctor signup error: {str(e)}")
        return jsonify({"error": str(e)}), 400


@doctor_bp.route('/login', methods=['POST', 'OPTIONS'])
def doctor_login():
    if request.method == 'OPTIONS':
        return '', 204

    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '')

    try:
        logger.info(f" Doctor login attempt for: {email}")
        
        user = auth.get_user_by_email(email)
        
        # Firebase Sign In
        sign_in_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={Config.FIREBASE_WEB_API_KEY}"
        sign_in_response = requests.post(sign_in_url, json={
            "email": email,
            "password": password,
            "returnSecureToken": True
        }, timeout=10)
        
        if sign_in_response.status_code != 200:
            logger.warning(f" Invalid credentials for doctor: {email}")
            return jsonify({"error": "Invalid credentials"}), 401
        
        id_token = sign_in_response.json().get('idToken')
        
        # Get doctor data from Firestore
        doctor_doc = db.collection('users').document(user.uid).get()
        
        if not doctor_doc.exists:
            logger.error(f" Doctor profile not found for: {email}")
            return jsonify({"error": "Doctor profile not found"}), 404
        
        doctor_data = doctor_doc.to_dict()
        
        # Check if doctor is verified (skip for admins)
        if doctor_data.get('role') == 'doctor' and not user.email_verified:
            logger.warning(f" Unverified doctor trying to login: {email}")
            return jsonify({"error": "Please verify your email first. Check your inbox. You have 12 hours to verify."}), 403
        
        # Accept both doctor and admin roles
        if doctor_data.get('role') not in ['doctor', 'admin']:
            logger.warning(f" Unauthorized login attempt: {email} (role: {doctor_data.get('role')})")
            return jsonify({"error": "Access denied. Only doctors and admins can login here."}), 403
        
        logger.info(f"Doctor login successful for: {email}")
        
        return jsonify({
            "message": "Login successful",
            "user": {
                "uid": user.uid,
                "email": user.email,
                "fullName": doctor_data.get('fullName'),
                "role": doctor_data.get('role'),
                "speciality": doctor_data.get('speciality'),
                "clinicName": doctor_data.get('clinicName'),
                "mobile": doctor_data.get('mobile')
            },
            "token": id_token
        }), 200
        
    except Exception as e:
        logger.error(f" Doctor login error: {str(e)}")
        return jsonify({"error": str(e)}), 400