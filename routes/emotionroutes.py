from flask import Blueprint, request, jsonify
from models.face_emotion import face_detector
from models.voice_emotion import voice_detector
from models.text_emotion import text_detector
import os
from werkzeug.utils import secure_filename
import tempfile
from config import db
import datetime
from firebase_admin import auth
from models.severity_model import severity_estimator

emotion_bp = Blueprint('emotion', __name__, url_prefix='/emotion')

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
ALLOWED_AUDIO_EXTENSIONS = {'wav', 'mp3', 'ogg', 'flac', 'm4a'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

def get_user_from_token(request):
    """Extract user from Firebase token"""
    try:
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None
        token = header.split('Bearer ', 1)[-1].strip()
        if not token:
            return None
        decoded = auth.verify_id_token(token)
        return decoded['uid']
    except:
        return None

def get_severity(emotion, confidence, all_emotions=None, source=None):
    """Estimate severity from the full AI emotion distribution."""
    return severity_estimator.predict(emotion, confidence, all_emotions, source)

def get_suggestion(emotion, severity_level):
    emotion = (emotion or '').lower()
    suggestions = {
        'happy': 'Keep smiling and note what helped you feel good today.',
        'joy': 'Keep smiling and note what helped you feel good today.',
        'neutral': 'Stay balanced. A short walk or hydration break can help maintain focus.',
        'sad': 'Try listening to calming music or talk to someone you trust.',
        'sadness': 'Try listening to calming music or talk to someone you trust.',
        'angry': 'Pause for a minute and take 5 slow deep breaths.',
        'anger': 'Pause for a minute and take 5 slow deep breaths.',
        'fear': 'Ground yourself by naming five things you can see around you.',
        'fearful': 'Ground yourself by naming five things you can see around you.',
        'disgust': 'Step away for a moment and reset with a calmer activity.',
        'disgusted': 'Step away for a moment and reset with a calmer activity.',
        'surprise': 'Take a moment to process what changed, then respond calmly.'
    }

    if severity_level == 'High':
        return 'Your stress signal looks high. Consider talking to the chatbot or finding a doctor.'
    return suggestions.get(emotion, 'Take a short break and check in with yourself.')

def get_multimodal_suggestion(severity_level):
    if severity_level == 'High':
        return 'Your combined face, voice, and text signals show high stress. Consider talking to the chatbot or finding a doctor.'
    if severity_level == 'Medium':
        return 'Your combined signals show moderate stress. Take a pause, breathe slowly, and check in again later.'
    return 'Your combined signals look stable. Keep tracking your mood and notice what supports you.'

def enrich_emotion_result(result, source):
    if not result or 'emotion' not in result:
        return result

    severity = get_severity(
        result.get('emotion'),
        result.get('confidence'),
        result.get('all_emotions'),
        source
    )
    enriched = {
        **result,
        'type': source,
        'severity': severity,
        'suggestion': get_suggestion(result.get('emotion'), severity['level']),
        'timestamp': datetime.datetime.now().isoformat(),
        'date': datetime.date.today().isoformat()
    }
    return enriched

def serialize_history_doc(data):
    serialized = dict(data)
    timestamp = serialized.get('timestamp')
    if hasattr(timestamp, 'isoformat'):
        serialized['timestamp'] = timestamp.isoformat()
    return serialized

def save_emotion_to_firebase(user_id, emotion_type, emotion_data):
    """Save emotion detection result to Firebase"""
    try:
        if not user_id:
            return False
        
        doc_ref = db.collection('users').document(user_id).collection('emotions')
        doc_ref.add({
            'type': emotion_type,  # 'face', 'voice', 'text'
            'emotion': emotion_data.get('emotion'),
            'confidence': emotion_data.get('confidence'),
            'all_emotions': emotion_data.get('all_emotions'),
            'severity': emotion_data.get('severity'),
            'suggestion': emotion_data.get('suggestion'),
            'face_detected': emotion_data.get('face_detected'),
            'timestamp': datetime.datetime.now(),
            'date': datetime.date.today().isoformat()
        })
        return True
    except Exception as e:
        print(f"Firebase save error: {e}")
        return False

def save_multimodal_severity_to_firebase(user_id, severity_data):
    """Save multimodal severity result to Firebase"""
    try:
        if not user_id:
            return False

        doc_ref = db.collection('users').document(user_id).collection('emotions')
        doc_ref.add({
            'type': 'multimodal',
            'emotion': severity_data.get('emotion'),
            'confidence': severity_data.get('confidence'),
            'all_emotions': severity_data.get('all_emotions'),
            'severity': severity_data.get('severity'),
            'suggestion': severity_data.get('suggestion'),
            'source_results': severity_data.get('source_results'),
            'timestamp': datetime.datetime.now(),
            'date': datetime.date.today().isoformat()
        })
        return True
    except Exception as e:
        print(f"Firebase multimodal save error: {e}")
        return False

# ============== FACE EMOTION ==============
@emotion_bp.route('/face/upload', methods=['POST'])
def detect_face_emotion_upload():
    """
    Detect emotion from uploaded image
    POST /emotion/face/upload
    Form-data: file (image)
    """
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        if not allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
            return jsonify({"error": "Invalid image format. Allowed: png, jpg, jpeg, gif, bmp"}), 400
        
        if face_detector is None:
            return jsonify({"error": "Face emotion model not loaded"}), 500
        
        # Save temp file
        filename = secure_filename(file.filename)
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        # Detect emotion
        result = enrich_emotion_result(face_detector.detect_emotion_from_image(tmp_path), 'face')
        
        # Save to Firebase if user logged in
        user_id = get_user_from_token(request)
        if user_id and 'emotion' in result:
            save_emotion_to_firebase(user_id, 'face', result)
        
        # Clean up
        os.remove(tmp_path)
        
        return jsonify(result), 200 if 'emotion' in result else 400
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@emotion_bp.route('/face/base64', methods=['POST'])
def detect_face_emotion_base64():
    """
    Detect emotion from base64 encoded image
    POST /emotion/face/base64
    JSON: { "image": "data:image/jpeg;base64," }
    """
    try:
        data = request.get_json()
        
        if not data or 'image' not in data:
            return jsonify({"error": "No image data provided"}), 400
        
        if face_detector is None:
            return jsonify({"error": "Face emotion model not loaded"}), 500
        
        result = enrich_emotion_result(face_detector.detect_emotion_from_base64(data['image']), 'face')
        
        # Save to Firebase
        user_id = get_user_from_token(request)
        if user_id and 'emotion' in result:
            save_emotion_to_firebase(user_id, 'face', result)
        
        return jsonify(result), 200 if 'emotion' in result else 400
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============== VOICE EMOTION ==============
@emotion_bp.route('/voice/upload', methods=['POST'])
def detect_voice_emotion_upload():
    """
    Detect emotion from uploaded audio
    POST /emotion/voice/upload
    Form-data: file (audio)
    """
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        if not allowed_file(file.filename, ALLOWED_AUDIO_EXTENSIONS):
            return jsonify({"error": "Invalid audio format. Allowed: wav, mp3, ogg, flac, m4a"}), 400
        
        if voice_detector is None:
            return jsonify({"error": "Voice emotion model not loaded"}), 500
        
        # Save temp file
        filename = secure_filename(file.filename)
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        # Detect emotion
        result = enrich_emotion_result(voice_detector.detect_emotion_from_file(tmp_path), 'voice')
        
        # Save to Firebase
        user_id = get_user_from_token(request)
        if user_id and 'emotion' in result:
            save_emotion_to_firebase(user_id, 'voice', result)
        
        # Clean up
        os.remove(tmp_path)
        
        return jsonify(result), 200 if 'emotion' in result else 400
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@emotion_bp.route('/voice/base64', methods=['POST'])
def detect_voice_emotion_base64():
    """
    Detect emotion from base64 encoded audio
    POST /emotion/voice/base64
    JSON: { "audio": "data:audio/wav;base64,...", "format": "wav" }
    """
    try:
        data = request.get_json()
        
        if not data or 'audio' not in data:
            return jsonify({"error": "No audio data provided"}), 400
        
        audio_format = data.get('format', 'wav')
        
        if voice_detector is None:
            return jsonify({"error": "Voice emotion model not loaded"}), 500
        
        result = enrich_emotion_result(voice_detector.detect_emotion_from_base64(data['audio'], audio_format), 'voice')
        
        # Save to Firebase
        user_id = get_user_from_token(request)
        if user_id and 'emotion' in result:
            save_emotion_to_firebase(user_id, 'voice', result)
        
        return jsonify(result), 200 if 'emotion' in result else 400
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============== TEXT EMOTION ==============
@emotion_bp.route('/text', methods=['POST'])
def detect_text_emotion():
    """
    Detect emotion from text
    POST /emotion/text
    JSON: { "text": "I am so happy today!" }
    """
    try:
        data = request.get_json()
        
        if not data or 'text' not in data:
            return jsonify({"error": "No text provided"}), 400
        
        text = data.get('text', '').strip()
        
        if not text:
            return jsonify({"error": "Text cannot be empty"}), 400
        
        if text_detector is None:
            return jsonify({"error": "Text emotion model not loaded"}), 500
        
        result = enrich_emotion_result(text_detector.detect_emotion(text), 'text')
        
        # Save to Firebase
        user_id = get_user_from_token(request)
        if user_id and 'emotion' in result:
            save_emotion_to_firebase(user_id, 'text', result)
        
        return jsonify(result), 200 if 'emotion' in result else 400
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@emotion_bp.route('/text/batch', methods=['POST'])
def detect_text_emotion_batch():
    """
    Detect emotions from multiple texts
    POST /emotion/text/batch
    JSON: { "texts": ["text1", "text2"] }
    """
    try:
        data = request.get_json()
        
        if not data or 'texts' not in data:
            return jsonify({"error": "No texts provided"}), 400
        
        texts = data.get('texts', [])
        
        if not isinstance(texts, list) or len(texts) == 0:
            return jsonify({"error": "Texts must be a non-empty list"}), 400
        
        if text_detector is None:
            return jsonify({"error": "Text emotion model not loaded"}), 500
        
        result = text_detector.detect_emotion_batch(texts)
        
        return jsonify(result), 200
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============== COMBINED ANALYSIS ==============
@emotion_bp.route('/combined', methods=['POST'])
def detect_combined_emotion():
    """
    Detect emotion from multiple sources (face + voice + text)
    POST /emotion/combined
    Form-data: image, audio, text (all optional)
    """
    try:
        results = {}
        
        # Process face
        if 'image' in request.files:
            file = request.files['image']
            if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
                filename = secure_filename(file.filename)
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
                    file.save(tmp.name)
                    tmp_path = tmp.name
                
                if face_detector:
                    results['face'] = enrich_emotion_result(face_detector.detect_emotion_from_image(tmp_path), 'face')
                os.remove(tmp_path)
        
        # Process voice
        if 'audio' in request.files:
            file = request.files['audio']
            if file and allowed_file(file.filename, ALLOWED_AUDIO_EXTENSIONS):
                filename = secure_filename(file.filename)
                with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
                    file.save(tmp.name)
                    tmp_path = tmp.name
                
                if voice_detector:
                    results['voice'] = enrich_emotion_result(voice_detector.detect_emotion_from_file(tmp_path), 'voice')
                os.remove(tmp_path)
        
        # Process text
        if 'text' in request.form:
            text = request.form.get('text', '').strip()
            if text and text_detector:
                results['text'] = enrich_emotion_result(text_detector.detect_emotion(text), 'text')
        
        if not results:
            return jsonify({"error": "No valid input provided"}), 400
        
        # Save to Firebase
        user_id = get_user_from_token(request)
        if user_id:
            for emotion_type, emotion_data in results.items():
                if 'emotion' in emotion_data:
                    save_emotion_to_firebase(user_id, emotion_type, emotion_data)
        
        # Aggregate results
        return jsonify({
            "results": results,
            "timestamp": datetime.datetime.now().isoformat()
        }), 200
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============== MULTIMODAL SEVERITY ==============
@emotion_bp.route('/severity/multimodal', methods=['POST'])
@emotion_bp.route('/emotion/severity/multimodal', methods=['POST'])
def detect_multimodal_severity():
    """
    Estimate severity only after face + voice + text are available.
    JSON: { "results": { "face": {...}, "voice": {...}, "text": {...} } }
    """
    try:
        data = request.get_json() or {}
        results = data.get('results') or {}
        required_sources = {'face', 'voice', 'text'}
        missing = [source for source in required_sources if not results.get(source)]

        if missing:
            return jsonify({
                "error": "Face, voice, and text results are required for severity.",
                "missing": missing
            }), 400

        severity_result = severity_estimator.predict_multimodal({
            'face': results.get('face'),
            'voice': results.get('voice'),
            'text': results.get('text')
        })
        severity_result['source_results'] = {
            'face': results.get('face'),
            'voice': results.get('voice'),
            'text': results.get('text')
        }
        severity_result['suggestion'] = get_multimodal_suggestion(
            severity_result['severity']['level']
        )
        severity_result['timestamp'] = datetime.datetime.now().isoformat()
        severity_result['date'] = datetime.date.today().isoformat()

        user_id = get_user_from_token(request)
        if user_id:
            save_multimodal_severity_to_firebase(user_id, severity_result)

        return jsonify(severity_result), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============== GET EMOTION HISTORY ==============
@emotion_bp.route('/history', methods=['GET'])
def get_emotion_history():
    """
    Get user's emotion detection history
    GET /emotion/history?days=7&type=face
    """
    try:
        user_id = get_user_from_token(request)
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401
        
        days = request.args.get('days', 7, type=int)
        emotion_type = request.args.get('type')  # Optional: 'face', 'voice', 'text'
        
        query = db.collection('users').document(user_id).collection('emotions')
        
        if emotion_type:
            query = query.where('type', '==', emotion_type)
        
        docs = query.order_by('timestamp', direction='DESCENDING').limit(100).stream()
        
        history = []
        for doc in docs:
            data = serialize_history_doc(doc.to_dict())
            data['id'] = doc.id
            history.append(data)
        
        return jsonify({"history": history, "count": len(history)}), 200
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Health check
@emotion_bp.route('/health', methods=['GET'])
def emotion_health():
    """Check if emotion detection models are loaded"""
    return jsonify({
        "status": "ok",
        "models": {
            "face": face_detector is not None,
            "voice": voice_detector is not None,
            "text": text_detector is not None
        }
    }), 200
