from flask import Blueprint, request, jsonify
from firebase_admin import auth, firestore
from config import db

admin_bp = Blueprint('admin', __name__)

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

def get_current_admin(req):
    uid = get_user_from_token(req)
    if not uid:
        return None, None

    doc = db.collection('users').document(uid).get()
    if not doc.exists:
        return uid, None

    profile = doc.to_dict()
    if profile.get('role') != 'admin':
        return uid, profile

    return uid, profile

def serialize_admin(uid, profile):
    full_name = profile.get('fullName') or 'Admin'
    return {
        "uid": uid,
        "adminId": uid,
        "email": profile.get('email') or '',
        "fullName": full_name,
        "name": full_name,
        "role": profile.get('role') or 'admin'
    }

def get_password_from_payload(data):
    return str(
        data.get('password')
        or data.get('newPassword')
        or data.get('confirmPassword')
        or ''
    ).strip()

def require_admin(req):
    uid, profile = get_current_admin(req)
    if not uid or not profile:
        return None, None, (jsonify({"error": "Not authenticated"}), 401)
    if profile.get('role') != 'admin':
        return uid, profile, (jsonify({"error": "Admin access required"}), 403)
    return uid, profile, None

# Get logged-in admin profile
@admin_bp.route('/me', methods=['GET'])
def get_logged_in_admin_profile():
    try:
        uid, profile, error = require_admin(request)
        if error:
            return error

        admin = serialize_admin(uid, profile)
        return jsonify({"admin": admin, **admin}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Update logged-in admin profile
@admin_bp.route('/me', methods=['PUT'])
def update_logged_in_admin_profile():
    try:
        uid, profile, error = require_admin(request)
        if error:
            return error

        data = request.json or {}
        full_name = str(data.get('fullName') or data.get('name') or profile.get('fullName') or '').strip()
        password = get_password_from_payload(data)

        if len(full_name) < 3:
            return jsonify({"error": "Full name must be at least 3 characters"}), 400
        if password and len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400

        db.collection('users').document(uid).update({
            'fullName': full_name,
            'updatedAt': firestore.SERVER_TIMESTAMP
        })
        auth.update_user(uid, display_name=full_name)

        if password:
            auth.update_user(uid, password=password)

        updated_profile = {**profile, "fullName": full_name}
        admin = serialize_admin(uid, updated_profile)
        return jsonify({
            "message": "Profile updated successfully",
            "admin": admin,
            **admin
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Get admin profile
@admin_bp.route('/profile/<admin_id>', methods=['GET', 'OPTIONS'])
def get_admin_profile(admin_id):
    if request.method == 'OPTIONS':
        return '', 204

    try:
        uid, profile, error = require_admin(request)
        if error:
            return error
        if uid != admin_id:
            return jsonify({"error": "You can update only your own admin profile"}), 403

        admin = serialize_admin(uid, profile)
        return jsonify({"admin": admin, **admin}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Update admin profile
@admin_bp.route('/profile/<admin_id>', methods=['POST', 'OPTIONS'])
def update_admin_profile(admin_id):
    if request.method == 'OPTIONS':
        return '', 204

    try:
        uid, profile, error = require_admin(request)
        if error:
            return error
        if uid != admin_id:
            return jsonify({"error": "You can update only your own admin profile"}), 403

        data = request.json or {}
        full_name = str(data.get('fullName') or data.get('name') or profile.get('fullName') or '').strip()
        password = get_password_from_payload(data)

        if len(full_name) < 3:
            return jsonify({"error": "Full name must be at least 3 characters"}), 400
        if password and len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400

        db.collection('users').document(admin_id).update({
            'fullName': full_name,
            'updatedAt': firestore.SERVER_TIMESTAMP
        })
        auth.update_user(admin_id, display_name=full_name)

        if password:
            auth.update_user(admin_id, password=password)

        updated_profile = {**profile, "fullName": full_name}
        admin = serialize_admin(admin_id, updated_profile)
        return jsonify({
            "message": "Profile updated successfully",
            "admin": admin,
            **admin
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Get all users
@admin_bp.route('/users', methods=['GET', 'OPTIONS'])
def get_all_users():
    try:
        users_ref = db.collection('users')
        users = users_ref.where('role', '==', 'user').stream()
        
        users_list = []
        for user in users:
            user_data = user.to_dict()
            users_list.append({
                'id': user.id,
                'name': user_data.get('fullName'),
                'email': user_data.get('email'),
                'joined': user_data.get('createdAt').strftime('%d %b %Y') if user_data.get('createdAt') else 'N/A',
                'mood': '😊',
                'status': 'Active'
            })
        
        return jsonify({"users": users_list}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Get all doctors
@admin_bp.route('/doctors', methods=['GET', 'OPTIONS'])
def get_all_doctors():
    try:
        doctors_ref = db.collection('users')
        doctors = doctors_ref.where('role', '==', 'doctor').stream()
        
        doctors_list = []
        for doctor in doctors:
            doctor_data = doctor.to_dict()
            doctors_list.append({
                'id': doctor.id,
                'name': doctor_data.get('fullName'),
                'email': doctor_data.get('email'),
                'exp': doctor_data.get('experience', 'N/A'),
                'spec': doctor_data.get('speciality'),
                'status': 'Available'
            })
        
        return jsonify({"doctors": doctors_list}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Delete user by ID
@admin_bp.route('/users/<user_id>', methods=['DELETE', 'OPTIONS'])
def delete_user(user_id):
    try:
        # Delete from Firebase Auth
        auth.delete_user(user_id)
        
        # Delete from Firestore
        db.collection('users').document(user_id).delete()
        
        return jsonify({"message": "User deleted successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Delete doctor by ID
@admin_bp.route('/doctors/<doctor_id>', methods=['DELETE', 'OPTIONS'])
def delete_doctor(doctor_id):
    try:
        # Delete from Firebase Auth
        auth.delete_user(doctor_id)
        
        # Delete from Firestore
        db.collection('users').document(doctor_id).collection('doctorAvailability').document('settings').delete()
        db.collection('users').document(doctor_id).delete()
        
        return jsonify({"message": "Doctor deleted successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Get dashboard stats
@admin_bp.route('/stats', methods=['GET', 'OPTIONS'])
def get_stats():
    try:
        users = len(list(db.collection('users').where('role', '==', 'user').stream()))
        doctors = len(list(db.collection('users').where('role', '==', 'doctor').stream()))
        
        return jsonify({
            "totalUsers": users,
            "totalDoctors": doctors,
            "serverHealth": "99.9%",
            "databaseLoad": "12%"
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400
