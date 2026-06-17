from flask import Blueprint, request, jsonify
from firebase_admin import auth, firestore
from google.cloud.exceptions import Conflict
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

doctor_bp = Blueprint('doctor_auth', __name__)

DEFAULT_AVAILABILITY = [
    {'day': 'Monday', 'active': True, 'slots': [{'start': '09:00', 'end': '17:00'}]},
    {'day': 'Tuesday', 'active': True, 'slots': [{'start': '09:00', 'end': '17:00'}]},
    {'day': 'Wednesday', 'active': True, 'slots': [{'start': '09:00', 'end': '17:00'}]},
    {'day': 'Thursday', 'active': True, 'slots': [{'start': '09:00', 'end': '17:00'}]},
    {'day': 'Friday', 'active': True, 'slots': [{'start': '09:00', 'end': '17:00'}]},
    {'day': 'Saturday', 'active': False, 'slots': [{'start': '09:00', 'end': '17:00'}]},
    {'day': 'Sunday', 'active': False, 'slots': [{'start': '09:00', 'end': '17:00'}]},
]

VISIBLE_APPOINTMENT_STATUSES = {'pending', 'approved', 'accepted', 'rejected', 'declined', 'cancelled'}
BOOKED_APPOINTMENT_STATUSES = {'pending', 'approved', 'accepted'}
APPOINTMENT_STATUS_ALIASES = {
    'accept': 'approved',
    'accepted': 'approved',
    'approve': 'approved',
    'decline': 'rejected',
    'declined': 'rejected',
    'refuse': 'rejected',
    'reject': 'rejected',
    'cancel': 'cancelled',
    'archive': 'archived',
    'restore': 'restored',
    'unarchive': 'restored'
}
ALLOWED_APPOINTMENT_STATUSES = VISIBLE_APPOINTMENT_STATUSES.union({'archived', 'restored'})

def normalize_date_slots(slots):
    return slots if isinstance(slots, list) and slots else [{'start': '09:00', 'end': '17:00'}]

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

def format_slot(slot):
    return normalize_time_slot(f"{slot.get('start')} - {slot.get('end')}")

def is_booked_status(status, previous_status=None):
    normalized_status = normalize_appointment_status(status) or 'pending'
    normalized_previous = normalize_appointment_status(previous_status)
    if normalized_status in BOOKED_APPOINTMENT_STATUSES:
        return True
    return normalized_status == 'archived' and normalized_previous in BOOKED_APPOINTMENT_STATUSES

def get_slot_lock_ref(doctor_id, appointment_date, time_slot):
    lock_id = get_slot_key(doctor_id, appointment_date, time_slot)
    return db.collection('appointmentSlots').document(lock_id)

def get_slot_key(doctor_id, appointment_date, time_slot):
    raw_key = f"{doctor_id}|{appointment_date}|{normalize_time_slot(time_slot)}"
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

def release_slot_lock(appointment_data):
    doctor_id = appointment_data.get('doctorId')
    appointment_date = appointment_data.get('date')
    time_slot = appointment_data.get('timeSlot')
    appointment_id = appointment_data.get('id')
    if not doctor_id or not appointment_date or not time_slot:
        return

    lock_ref = get_slot_lock_ref(doctor_id, appointment_date, time_slot)
    lock_doc = lock_ref.get()
    if not lock_doc.exists:
        return
    if appointment_id and lock_doc.to_dict().get('appointmentId') != appointment_id:
        return
    lock_ref.delete()

def sync_slot_lock_for_status(appointment_id, appointment_data, status, previous_status=None):
    lock_data = {**appointment_data, "id": appointment_id}
    if is_booked_status(status, previous_status):
        lock_ref = get_slot_lock_ref(
            appointment_data.get('doctorId'),
            appointment_data.get('date'),
            appointment_data.get('timeSlot')
        )
        lock_ref.set({
            "appointmentId": appointment_id,
            "doctorId": appointment_data.get('doctorId'),
            "date": appointment_data.get('date'),
            "timeSlot": appointment_data.get('timeSlot'),
            "status": status,
            "previousStatus": previous_status,
            "updatedAt": firestore.SERVER_TIMESTAMP
        })
    else:
        release_slot_lock(lock_data)

@firestore.transactional
def create_appointment_with_slot_lock(transaction, slot_lock_ref, appointment_ref, appointment):
    slot_doc = slot_lock_ref.get(transaction=transaction)
    if slot_doc.exists and is_booked_status(
        slot_doc.to_dict().get('status'),
        slot_doc.to_dict().get('previousStatus')
    ):
        return False
    appointment_doc = appointment_ref.get(transaction=transaction)
    if appointment_doc.exists and is_booked_status(
        appointment_doc.to_dict().get('status'),
        appointment_doc.to_dict().get('previousStatus')
    ):
        return False

    transaction.set(slot_lock_ref, {
        "appointmentId": appointment_ref.id,
        "doctorId": appointment.get('doctorId'),
        "date": appointment.get('date'),
        "timeSlot": appointment.get('timeSlot'),
        "status": appointment.get('status'),
        "createdAt": firestore.SERVER_TIMESTAMP
    })
    transaction.set(appointment_ref, appointment)
    return True

def get_booked_slots_by_date(doctor_id):
    booked_slots = {}
    docs = db.collection('appointments').where('doctorId', '==', doctor_id).stream()

    for doc in docs:
        data = doc.to_dict()
        if not is_booked_status(data.get('status'), data.get('previousStatus')):
            continue
        appointment_date = data.get('date')
        time_slot = data.get('timeSlot')
        if not appointment_date or not time_slot:
            continue
        booked_slots.setdefault(appointment_date, set()).add(normalize_time_slot(time_slot))

    return booked_slots

def has_existing_booked_appointment(doctor_id, appointment_date, time_slot):
    target_slot = normalize_time_slot(time_slot)
    docs = (db.collection('appointments')
            .where('doctorId', '==', doctor_id)
            .where('date', '==', appointment_date)
            .stream())

    for doc in docs:
        data = doc.to_dict()
        if normalize_time_slot(data.get('timeSlot')) != target_slot:
            continue
        if is_booked_status(data.get('status'), data.get('previousStatus')):
            return True

    return False

def remove_booked_slots(available_dates, booked_slots_by_date):
    filtered_dates = []
    for item in available_dates:
        booked_slots = booked_slots_by_date.get(item.get('date'), set())
        slots = [
            slot
            for slot in item.get('slots', [])
            if format_slot(slot) not in booked_slots
        ]
        if slots:
            filtered_dates.append({
                **item,
                "slots": slots,
                "bookedSlots": sorted(booked_slots)
            })

    return filtered_dates

def mark_booked_slots(available_dates, booked_slots_by_date):
    marked_dates = []
    for item in available_dates:
        booked_slots = booked_slots_by_date.get(item.get('date'), set())
        slots = []
        for slot in item.get('slots', []):
            slot_text = format_slot(slot)
            slots.append({
                **slot,
                "label": slot_text,
                "timeSlot": slot_text,
                "booked": slot_text in booked_slots,
                "status": "booked" if slot_text in booked_slots else "available"
            })

        marked_dates.append({
            **item,
            "slots": slots,
            "bookedSlots": sorted(booked_slots)
        })

    return marked_dates

def get_available_dates(availability, custom_dates=None, days_ahead=60):
    if isinstance(custom_dates, list):
        dates = []
        today = datetime.utcnow().date()

        for item in custom_dates:
            if not item.get('active', True) or not item.get('date'):
                continue
            try:
                day = datetime.strptime(item.get('date'), '%Y-%m-%d').date()
            except ValueError:
                continue
            if day < today:
                continue
            dates.append({
                "date": day.isoformat(),
                "label": day.strftime('%d %b %Y'),
                "day": day.strftime('%A'),
                "slots": normalize_date_slots(item.get('slots'))
            })

        return sorted(dates, key=lambda item: item['date'])

    active_days = {
        item.get('day')
        for item in (availability or DEFAULT_AVAILABILITY)
        if item.get('active')
    }
    dates = []
    today = datetime.utcnow().date()

    for offset in range(days_ahead):
        day = today + timedelta(days=offset)
        day_name = day.strftime('%A')
        if day_name in active_days:
            day_availability = next(
                (item for item in (availability or DEFAULT_AVAILABILITY) if item.get('day') == day_name),
                {}
            )
            dates.append({
                "date": day.isoformat(),
                "label": day.strftime('%d %b %Y'),
                "day": day_name,
                "slots": normalize_date_slots(day_availability.get('slots'))
            })

    return dates

def is_date_available(availability, appointment_date, custom_dates=None):
    try:
        selected_day = datetime.strptime(appointment_date, '%Y-%m-%d').strftime('%A')
    except ValueError:
        return False

    if isinstance(custom_dates, list):
        return any(
            item.get('date') == appointment_date and item.get('active', True)
            for item in custom_dates
        )

    return any(
        item.get('day') == selected_day and item.get('active')
        for item in (availability or DEFAULT_AVAILABILITY)
    )

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

def normalize_appointment_status(status):
    if not status:
        return None
    normalized = str(status).strip().lower()
    return APPOINTMENT_STATUS_ALIASES.get(normalized, normalized)

def get_doctor_actions(status):
    if status == 'pending':
        return ['accept', 'reject']
    if status in ['approved', 'accepted']:
        return ['cancel', 'archive']
    if status in ['rejected', 'declined', 'cancelled']:
        return ['archive']
    if status == 'archived':
        return ['restore']
    return []

def get_current_profile(req):
    uid = get_user_from_token(req)
    if not uid:
        return None, None
    doc = db.collection('users').document(uid).get()
    if not doc.exists:
        return uid, None
    return uid, doc.to_dict()

def require_doctor(req):
    uid, profile = get_current_profile(req)
    if not uid or not profile:
        return None, None, (jsonify({"error": "Not authenticated"}), 401)
    if profile.get('role') not in ['doctor', 'admin']:
        return uid, profile, (jsonify({"error": "Doctor access required"}), 403)
    return uid, profile, None

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


@doctor_bp.route('/list', methods=['GET'])
def list_doctors():
    """Public list of doctors for the user booking page."""
    try:
        docs = db.collection('users').where('role', '==', 'doctor').stream()
        doctors = []

        for doc in docs:
            data = serialize_doc(doc.to_dict())
            availability = data.get('availability') or DEFAULT_AVAILABILITY
            custom_dates = data.get('availableDates')
            available_dates = get_available_dates(availability, custom_dates)
            available_dates = mark_booked_slots(available_dates, get_booked_slots_by_date(doc.id))
            doctors.append({
                "uid": doc.id,
                "name": data.get('fullName') or 'Doctor',
                "email": data.get('email'),
                "role": data.get('speciality') or 'Mental Health Professional',
                "speciality": data.get('speciality') or 'Mental Health Professional',
                "clinicName": data.get('clinicName') or '',
                "phone": data.get('mobile') or '',
                "desc": data.get('description') or 'Available for emotional wellness and stress support sessions.',
                "availability": availability,
                "availableDates": available_dates
            })

        return jsonify({"doctors": doctors, "count": len(doctors)}), 200
    except Exception as e:
        logger.error(f" Doctor list error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/appointments', methods=['POST'])
def create_appointment():
    """Create an appointment request from a logged-in user."""
    try:
        user_id, user_profile = get_current_profile(request)
        if not user_id or not user_profile:
            return jsonify({"error": "Not authenticated"}), 401

        data = request.json or {}
        doctor_id = data.get('doctorId')
        appointment_date = data.get('date')
        time_slot = normalize_time_slot(data.get('timeSlot'))
        patient_email = data.get('email') or user_profile.get('email')

        if not doctor_id or not appointment_date or not time_slot:
            return jsonify({"error": "Doctor, date, and time slot are required"}), 400

        doctor_doc = db.collection('users').document(doctor_id).get()
        if not doctor_doc.exists or doctor_doc.to_dict().get('role') != 'doctor':
            return jsonify({"error": "Doctor not found"}), 404

        doctor_data = doctor_doc.to_dict()
        doctor_availability = doctor_data.get('availability') or DEFAULT_AVAILABILITY
        doctor_dates = doctor_data.get('availableDates')
        available_dates = remove_booked_slots(
            get_available_dates(doctor_availability, doctor_dates),
            get_booked_slots_by_date(doctor_id)
        )
        selected_date = next((item for item in available_dates if item.get('date') == appointment_date), None)
        if not selected_date or not is_date_available(doctor_availability, appointment_date, doctor_dates):
            return jsonify({"error": "Doctor is not available on the selected date"}), 400

        available_slots = {format_slot(slot) for slot in selected_date.get('slots', [])}
        if time_slot not in available_slots:
            return jsonify({"error": "This time slot is already booked. Please select another slot."}), 409
        if has_existing_booked_appointment(doctor_id, appointment_date, time_slot):
            return jsonify({"error": "This time slot is already booked. Please select another slot."}), 409

        slot_lock_ref = get_slot_lock_ref(doctor_id, appointment_date, time_slot)
        slot_key = slot_lock_ref.id
        appointment_ref = db.collection('appointments').document(slot_lock_ref.id)
        appointment = {
            "doctorId": doctor_id,
            "doctorName": doctor_data.get('fullName'),
            "patientId": user_id,
            "patientName": user_profile.get('fullName') or user_profile.get('email'),
            "patientEmail": patient_email,
            "specialty": doctor_data.get('speciality') or 'Consultation',
            "date": appointment_date,
            "timeSlot": time_slot,
            "slotKey": slot_key,
            "status": "pending",
            "createdAt": firestore.SERVER_TIMESTAMP
        }
        logger.info(
            "Booking slotKey=%s doctorId=%s date=%s timeSlot=%s patient=%s",
            slot_key,
            doctor_id,
            appointment_date,
            time_slot,
            patient_email
        )
        try:
            appointment_ref.create(appointment)
        except Conflict:
            return jsonify({"error": "This time slot is already booked. Please select another slot."}), 409

        slot_lock_ref.set({
            "appointmentId": appointment_ref.id,
            "doctorId": doctor_id,
            "date": appointment_date,
            "timeSlot": time_slot,
            "slotKey": slot_key,
            "status": "pending",
            "createdAt": firestore.SERVER_TIMESTAMP
        })

        return jsonify({
            "message": "Appointment request sent",
            "appointment": {**appointment, "id": appointment_ref.id, "createdAt": datetime.utcnow().isoformat()}
        }), 201
    except Exception as e:
        logger.error(f" Appointment create error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/me', methods=['GET'])
def get_doctor_profile():
    try:
        uid, profile, error = require_doctor(request)
        if error:
            return error

        return jsonify({
            "doctor": {
                "uid": uid,
                "name": profile.get('fullName') or 'Doctor',
                "email": profile.get('email'),
                "specialty": profile.get('speciality') or '',
                "clinicName": profile.get('clinicName') or '',
                "mobile": profile.get('mobile') or '',
                "availability": profile.get('availability') or DEFAULT_AVAILABILITY,
                "availableDates": profile.get('availableDates') or []
            }
        }), 200
    except Exception as e:
        logger.error(f" Doctor profile fetch error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/me', methods=['PUT'])
def update_doctor_profile():
    try:
        uid, profile, error = require_doctor(request)
        if error:
            return error

        data = request.json or {}
        updates = {
            "fullName": data.get('name', profile.get('fullName')),
            "speciality": data.get('specialty', profile.get('speciality')),
            "clinicName": data.get('clinicName', profile.get('clinicName')),
            "mobile": data.get('mobile', profile.get('mobile')),
            "updatedAt": firestore.SERVER_TIMESTAMP
        }
        db.collection('users').document(uid).update(updates)

        if data.get('password'):
            auth.update_user(uid, password=data.get('password'))

        return jsonify({"message": "Profile updated successfully"}), 200
    except Exception as e:
        logger.error(f" Doctor profile update error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/availability', methods=['GET'])
def get_doctor_availability():
    try:
        uid, profile, error = require_doctor(request)
        if error:
            return error
        return jsonify({
            "availability": profile.get('availability') or DEFAULT_AVAILABILITY,
            "availableDates": profile.get('availableDates') or []
        }), 200
    except Exception as e:
        logger.error(f" Availability fetch error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/availability', methods=['PUT'])
def update_doctor_availability():
    try:
        uid, profile, error = require_doctor(request)
        if error:
            return error

        data = request.json or {}
        availability = data.get('availability')
        if not isinstance(availability, list):
            return jsonify({"error": "Availability must be a list"}), 400

        available_dates = data.get('availableDates')
        if available_dates is not None and not isinstance(available_dates, list):
            return jsonify({"error": "Available dates must be a list"}), 400

        updates = {
            "availability": availability,
            "updatedAt": firestore.SERVER_TIMESTAMP
        }
        if available_dates is not None:
            updates["availableDates"] = available_dates

        db.collection('users').document(uid).update({
            **updates
        })
        return jsonify({
            "message": "Availability updated",
            "availability": availability,
            "availableDates": available_dates or []
        }), 200
    except Exception as e:
        logger.error(f" Availability update error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/dashboard/appointments', methods=['GET'])
def get_doctor_appointments():
    try:
        uid, profile, error = require_doctor(request)
        if error:
            return error

        include_archived = request.args.get('includeArchived', '').lower() == 'true'

        docs = (db.collection('appointments')
                .where('doctorId', '==', uid)
                .stream())

        appointments = []
        for doc in docs:
            data = serialize_doc(doc.to_dict())
            status = normalize_appointment_status(data.get('status')) or 'pending'
            if not include_archived and (status == 'archived' or data.get('archived')):
                continue

            appointments.append({
                "id": doc.id,
                "patient": data.get('patientName') or data.get('patientEmail') or 'Patient',
                "patientEmail": data.get('patientEmail'),
                "specialty": data.get('specialty') or 'Consultation',
                "date": data.get('date'),
                "time": data.get('timeSlot'),
                "slotKey": data.get('slotKey') or get_slot_key(uid, data.get('date'), data.get('timeSlot')),
                "status": status,
                "previousStatus": normalize_appointment_status(data.get('previousStatus')),
                "createdAt": data.get('createdAt'),
                "actions": get_doctor_actions(status)
            })

        appointments.sort(key=lambda item: item.get('createdAt') or '', reverse=True)
        return jsonify({"appointments": appointments, "count": len(appointments)}), 200
    except Exception as e:
        logger.error(f" Doctor appointments fetch error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@doctor_bp.route('/appointments/<appointment_id>/status', methods=['PUT'])
def update_appointment_status(appointment_id):
    try:
        uid, profile, error = require_doctor(request)
        if error:
            return error

        data = request.json or {}
        status = normalize_appointment_status(data.get('status') or data.get('action'))
        if status not in ALLOWED_APPOINTMENT_STATUSES:
            return jsonify({
                "error": "Invalid status",
                "allowedStatuses": sorted(ALLOWED_APPOINTMENT_STATUSES)
            }), 400

        appointment_ref = db.collection('appointments').document(appointment_id)
        appointment_doc = appointment_ref.get()
        if not appointment_doc.exists:
            return jsonify({"error": "Appointment not found"}), 404
        appointment_data = appointment_doc.to_dict()
        if appointment_data.get('doctorId') != uid:
            return jsonify({"error": "You can update only your appointments"}), 403

        if status == 'restored':
            previous_status = normalize_appointment_status(appointment_data.get('previousStatus')) or 'pending'
            if previous_status in ['archived', 'restored']:
                previous_status = 'pending'
            if previous_status not in VISIBLE_APPOINTMENT_STATUSES:
                previous_status = 'pending'
            status = previous_status

        updates = {
            "status": status,
            "updatedAt": firestore.SERVER_TIMESTAMP
        }
        if status == 'archived':
            updates.update({
                "archived": True,
                "archivedAt": firestore.SERVER_TIMESTAMP,
                "previousStatus": normalize_appointment_status(appointment_data.get('status')) or 'pending'
            })
        else:
            updates["archived"] = False

        appointment_ref.update(updates)
        sync_slot_lock_for_status(
            appointment_id,
            appointment_data,
            status,
            updates.get('previousStatus') or appointment_data.get('previousStatus')
        )
        return jsonify({"message": "Appointment updated", "status": status}), 200
    except Exception as e:
        logger.error(f" Appointment status update error: {str(e)}")
        return jsonify({"error": str(e)}), 500
