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

user_bp = Blueprint('user_auth', __name__)

# Helper function to delete unverified users after 12 hours
def delete_unverified_user_after_12h(uid, email):
    """Delete unverified user after 12 hours (43200 seconds)"""
    def delayed_delete():
        try:
            time.sleep(43200)  # 12 hours = 12 × 60 × 60 = 43200 seconds
            user = auth.get_user(uid)
            
            if not user.email_verified:
                auth.delete_user(uid)
                db.collection('users').document(uid).delete()
                logger.info(f" Unverified user {email} deleted after 12 hours")
        except Exception as e:
            logger.error(f" Error deleting unverified user {email}: {str(e)}")
    
    thread = threading.Thread(target=delayed_delete, daemon=True)
    thread.start()

@user_bp.route('/signup', methods=['POST', 'OPTIONS'])
def user_signup():
    if request.method == 'OPTIONS':
        return '', 204

    data = request.json
    full_name = data.get('fullName', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')

    try:
        logger.info(f" Creating user account for: {email}")
        
        user = auth.create_user(email=email, password=password, display_name=full_name)
        is_admin = (email.lower() == Config.ADMIN_EMAIL.lower())
        role = 'admin' if is_admin else 'user'

        # Email Verification
        if not is_admin:
            logger.info(f" Generating ID token for: {email}")
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
            
            logger.info(f" Sending verification email to: {email}")
            verify_response = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={Config.FIREBASE_WEB_API_KEY}",
                json=verify_payload,
                timeout=10
            )
            
            logger.info(f" Verification email response status: {verify_response.status_code}")
            
            if verify_response.status_code == 200:
                logger.info(f" Verification email sent successfully to {email}")
                delete_unverified_user_after_12h(user.uid, email)
            else:
                logger.error(f" Email send failed: {verify_response.json()}")
                auth.delete_user(user.uid)
                return jsonify({"error": "Failed to send verification email"}), 400
        else:
            logger.info(f"👑 Admin account created: {email}")
            auth.update_user(user.uid, email_verified=True)

        # Save user to Firestore
        db.collection('users').document(user.uid).set({
            'fullName': full_name,
            'email': email,
            'role': role,
            'createdAt': firestore.SERVER_TIMESTAMP,
            'emailVerified': False if not is_admin else True,
            'verificationDeadline': datetime.utcnow() + timedelta(hours=12) if not is_admin else None
        })

        message = "Admin account created!" if is_admin else "User registered! Check your email for verification link. You have 12 hours to verify."
        return jsonify({
            "message": message,
            "user": {
                "uid": user.uid,
                "email": email,
                "fullName": full_name,
                "role": role
            }
        }), 201
        
    except Exception as e:
        logger.error(f" Signup error: {str(e)}")
        return jsonify({"error": str(e)}), 400


@user_bp.route('/login', methods=['POST', 'OPTIONS'])
def user_login():
    if request.method == 'OPTIONS':
        return '', 204

    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '')

    try:
        logger.info(f" Login attempt for: {email}")
        
        user = auth.get_user_by_email(email)
        
        # Firebase Sign In
        sign_in_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={Config.FIREBASE_WEB_API_KEY}"
        sign_in_response = requests.post(sign_in_url, json={
            "email": email,
            "password": password,
            "returnSecureToken": True
        }, timeout=10)
        
        if sign_in_response.status_code != 200:
            logger.warning(f" Invalid credentials for: {email}")
            return jsonify({"error": "Invalid credentials"}), 401
        
        id_token = sign_in_response.json().get('idToken')
        
        # Get user data from Firestore
        user_doc = db.collection('users').document(user.uid).get()
        
        if not user_doc.exists:
            logger.error(f" User profile not found for: {email}")
            return jsonify({"error": "User profile not found"}), 404
        
        user_data = user_doc.to_dict()
        
        # Check if user is verified (skip for admins)
        if user_data.get('role') == 'user' and not user.email_verified:
            logger.warning(f" Unverified user trying to login: {email}")
            return jsonify({"error": "Please verify your email first. Check your inbox. You have 12 hours to verify."}), 403
        
        logger.info(f" Login successful for: {email}")
        
        return jsonify({
            "message": "Login successful",
            "user": {
                "uid": user.uid,
                "email": user.email,
                "fullName": user_data.get('fullName'),
                "role": user_data.get('role')
            },
            "token": id_token
        }), 200
        
    except Exception as e:
        logger.error(f" Login error: {str(e)}")
        return jsonify({"error": str(e)}), 400