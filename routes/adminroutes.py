from flask import Blueprint, request, jsonify
from firebase_admin import auth, firestore
from config import db, Config
from datetime import datetime

admin_bp = Blueprint('admin', __name__)

# Get admin profile
@admin_bp.route('/profile/<admin_id>', methods=['GET', 'OPTIONS'])
def get_admin_profile(admin_id):
    try:
        admin_doc = db.collection('users').document(admin_id).get()
        
        if not admin_doc.exists:
            return jsonify({"error": "Admin not found"}), 404
        
        admin_data = admin_doc.to_dict()
        return jsonify({
            "adminId": admin_id,
            "name": admin_data.get('fullName', 'Admin'),
            "email": admin_data.get('email', '')
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Update admin profile
@admin_bp.route('/profile/<admin_id>', methods=['POST', 'OPTIONS'])
def update_admin_profile(admin_id):
    try:
        data = request.json
        
        # Update Firestore
        db.collection('users').document(admin_id).update({
            'fullName': data.get('name'),
            'email': data.get('email'),
            'updatedAt': datetime.utcnow()
        })
        
        # Update Firebase Auth password agar diya gaya ho
        if data.get('password') and len(data.get('password', '')) >= 6:
            try:
                auth.update_user(admin_id, password=data.get('password'))
            except Exception as pwd_error:
                return jsonify({"error": f"Password update failed: {str(pwd_error)}"}), 400
        
        return jsonify({
            "adminId": admin_id,
            "name": data.get('name'),
            "email": data.get('email'),
            "message": "Profile updated successfully"
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

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