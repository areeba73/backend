from flask import Blueprint, request, jsonify
from firebase_admin import auth, firestore
import requests
from config import db, Config
from datetime import datetime, timedelta
import threading
import time
import logging
import hashlib
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

user_bp = Blueprint('user_auth', __name__)

def get_user_from_token(req):
    try:
        header = req.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None
        token = header.split('Bearer ', 1)[-1].strip()
        if not token:
            return None
        decoded = auth.verify_id_token(token)
        return decoded['uid']
    except Exception:
        return None

def serialize_doc(data):
    serialized = dict(data)
    for key, value in list(serialized.items()):
        if hasattr(value, 'isoformat'):
            serialized[key] = value.isoformat()
    return serialized

def normalize_time_slot(time_slot):
    if isinstance(time_slot, dict):
        time_slot = f"{time_slot.get('start')} - {time_slot.get('end')}"
    normalized = re.sub(r'\s+', ' ', str(time_slot or '').strip())
    normalized = re.sub(r'\s*-\s*', ' - ', normalized)
    parts = [part.strip() for part in normalized.split(' - ', 1)]
    if len(parts) != 2:
        return normalized

    return f"{normalize_clock_time(parts[0])} - {normalize_clock_time(parts[1])}"

def normalize_clock_time(value):
    text = str(value or '').strip().upper().replace('.', '')
    match = re.match(r'^(\d{1,2})(?::(\d{1,2}))?\s*(AM|PM)?$', text)
    if not match:
        return re.sub(r'\s+', ' ', str(value or '').strip())

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem == 'AM' and hour == 12:
        hour = 0
    elif meridiem == 'PM' and hour != 12:
        hour += 12

    return f"{hour:02d}:{minute:02d}"

def get_slot_lock_ref(doctor_id, appointment_date, time_slot):
    raw_key = f"{doctor_id}|{appointment_date}|{normalize_time_slot(time_slot)}"
    lock_id = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
    return db.collection('appointmentSlots').document(lock_id)

def release_slot_lock(appointment_id, appointment_data):
    doctor_id = appointment_data.get('doctorId')
    appointment_date = appointment_data.get('date')
    time_slot = appointment_data.get('timeSlot')
    if not doctor_id or not appointment_date or not time_slot:
        return

    lock_ref = get_slot_lock_ref(doctor_id, appointment_date, time_slot)
    lock_doc = lock_ref.get()
    if not lock_doc.exists:
        return
    if lock_doc.to_dict().get('appointmentId') != appointment_id:
        return
    lock_ref.delete()

def get_current_profile(req):
    uid = get_user_from_token(req)
    if not uid:
        return None, None
    doc = db.collection('users').document(uid).get()
    if not doc.exists:
        return uid, None
    return uid, doc.to_dict()

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


@user_bp.route('/me', methods=['GET'])
def get_user_profile():
    """Fetch the logged-in user's profile."""
    try:
        uid, profile = get_current_profile(request)
        if not uid or not profile:
            return jsonify({"error": "Not authenticated"}), 401

        if profile.get('role') != 'user':
            return jsonify({"error": "User access required"}), 403

        return jsonify({
            "user": {
                "uid": uid,
                "email": profile.get('email'),
                "fullName": profile.get('fullName') or '',
                "role": profile.get('role') or 'user'
            }
        }), 200
    except Exception as e:
        logger.error(f" User profile fetch error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/me', methods=['PUT'])
def update_user_profile():
    """Update the logged-in user's profile."""
    try:
        uid, profile = get_current_profile(request)
        if not uid or not profile:
            return jsonify({"error": "Not authenticated"}), 401

        if profile.get('role') != 'user':
            return jsonify({"error": "User access required"}), 403

        data = request.json or {}
        full_name = str(data.get('fullName') or profile.get('fullName') or '').strip()
        password = str(data.get('password') or '').strip()

        if len(full_name) < 3:
            return jsonify({"error": "Full name must be at least 3 characters"}), 400
        if password and len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400

        db.collection('users').document(uid).update({
            "fullName": full_name,
            "updatedAt": firestore.SERVER_TIMESTAMP
        })
        auth.update_user(uid, display_name=full_name)

        if password:
            auth.update_user(uid, password=password)

        return jsonify({
            "message": "Profile updated successfully",
            "user": {
                "uid": uid,
                "email": profile.get('email'),
                "fullName": full_name,
                "role": profile.get('role') or 'user'
            }
        }), 200
    except Exception as e:
        logger.error(f" User profile update error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/profile/<user_id>', methods=['POST', 'OPTIONS'])
def update_user_profile_by_id(user_id):
    """Admin-style compatibility route for updating a user's own profile."""
    if request.method == 'OPTIONS':
        return '', 204

    try:
        uid, profile = get_current_profile(request)
        if not uid or not profile:
            return jsonify({"error": "Not authenticated"}), 401

        if uid != user_id or profile.get('role') != 'user':
            return jsonify({"error": "You can update only your own user profile"}), 403

        data = request.json or {}
        full_name = str(data.get('fullName') or data.get('name') or profile.get('fullName') or '').strip()
        password = str(data.get('password') or '').strip()

        if len(full_name) < 3:
            return jsonify({"error": "Full name must be at least 3 characters"}), 400
        if password and len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400

        db.collection('users').document(uid).update({
            "fullName": full_name,
            "updatedAt": firestore.SERVER_TIMESTAMP
        })
        auth.update_user(uid, display_name=full_name)

        if password:
            auth.update_user(uid, password=password)

        return jsonify({
            "message": "Profile updated successfully",
            "user": {
                "uid": uid,
                "email": profile.get('email'),
                "fullName": full_name,
                "role": profile.get('role') or 'user'
            }
        }), 200
    except Exception as e:
        logger.error(f" User profile compatibility update error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/appointments', methods=['GET'])
def get_user_appointments():
    """Fetch appointment requests booked by the logged-in user."""
    try:
        uid, profile = get_current_profile(request)
        if not uid or not profile:
            return jsonify({"error": "Not authenticated"}), 401

        appointment_docs = {}
        patient_id_docs = (db.collection('appointments')
                           .where('patientId', '==', uid)
                           .stream())
        for doc in patient_id_docs:
            appointment_docs[doc.id] = doc

        user_email = profile.get('email')
        if user_email:
            patient_email_docs = (db.collection('appointments')
                                  .where('patientEmail', '==', user_email)
                                  .stream())
            for doc in patient_email_docs:
                appointment_docs[doc.id] = doc

        appointments = []
        for doc in appointment_docs.values():
            data = serialize_doc(doc.to_dict())
            status = data.get('status') or 'pending'
            if status == 'archived':
                status = data.get('previousStatus') or 'pending'
            appointments.append({
                "id": doc.id,
                "doctor": data.get('doctorName') or 'Doctor',
                "doctorId": data.get('doctorId'),
                "specialty": data.get('specialty') or 'Consultation',
                "date": data.get('date'),
                "time": data.get('timeSlot'),
                "status": status,
                "createdAt": data.get('createdAt')
            })

        appointments.sort(key=lambda item: item.get('createdAt') or '', reverse=True)
        return jsonify({"appointments": appointments, "count": len(appointments)}), 200
    except Exception as e:
        logger.error(f" User appointments fetch error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@user_bp.route('/appointments/<appointment_id>', methods=['DELETE'])
def delete_user_appointment(appointment_id):
    """Delete an appointment booked by the logged-in user."""
    try:
        uid, profile = get_current_profile(request)
        if not uid or not profile:
            return jsonify({"error": "Not authenticated"}), 401

        appointment_ref = db.collection('appointments').document(appointment_id)
        appointment_doc = appointment_ref.get()
        if not appointment_doc.exists:
            return jsonify({"error": "Appointment not found"}), 404

        data = appointment_doc.to_dict()
        user_email = profile.get('email')
        owns_appointment = data.get('patientId') == uid or (
            user_email and data.get('patientEmail') == user_email
        )
        if not owns_appointment:
            return jsonify({"error": "You can delete only your appointments"}), 403

        appointment_ref.delete()
        release_slot_lock(appointment_id, data)
        return jsonify({"message": "Appointment deleted successfully"}), 200
    except Exception as e:
        logger.error(f" User appointment delete error: {str(e)}")
        return jsonify({"error": str(e)}), 500
