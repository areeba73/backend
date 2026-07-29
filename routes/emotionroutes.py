from flask import Blueprint, request, jsonify
from models.face_emotion import face_detector
from models.voice_emotion import VOICE_ENGINE, voice_detector
from models.text_emotion import text_detector
import os
import re
import requests
from werkzeug.utils import secure_filename
import tempfile
from config import db
import datetime
import threading
from firebase_admin import auth
from models.severity_model import severity_estimator

emotion_bp = Blueprint('emotion', __name__, url_prefix='/emotion')

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}
ALLOWED_AUDIO_EXTENSIONS = {'wav', 'mp3', 'ogg', 'flac', 'm4a', 'webm'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_RECOMMENDATION_MODEL = "gemini-2.5-flash"
_gemini_session = requests.Session()
_gemini_session.trust_env = False
_gemini_session.proxies = {}

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

def clean_ai_recommendation(text):
    cleaned = re.sub(r'\s+', ' ', str(text or '')).strip()
    cleaned = re.sub(r'^[\-\*\d\.\)\s]+', '', cleaned)
    return cleaned.strip('"\' ')

def summarize_emotion_distribution(all_emotions):
    if not isinstance(all_emotions, dict):
        return "not available"

    def as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    top_items = sorted(
        all_emotions.items(),
        key=lambda item: as_float(item[1]),
        reverse=True
    )[:3]
    return ", ".join(f"{name}: {as_float(score):.2f}" for name, score in top_items) or "not available"

def summarize_source_results(source_results):
    if not isinstance(source_results, dict):
        return "not available"

    summaries = []
    for source, data in source_results.items():
        if not isinstance(data, dict):
            continue
        severity = data.get('severity') or {}
        summaries.append(
            f"{source}: mood={data.get('emotion') or 'unknown'}, "
            f"confidence={data.get('confidence') or 'unknown'}, "
            f"severity={severity.get('level') or 'unknown'}"
        )
    return "; ".join(summaries) or "not available"

def fallback_recommendation(emotion, severity=None, source=None):
    normalized_emotion = str(emotion or 'unknown').strip().lower()
    level = str((severity or {}).get('level') or '').lower()
    source_phrases = {
        'face': 'Your facial expression looks',
        'voice': 'Your voice sounds',
        'text': 'Your words suggest',
        'multimodal': 'Your combined scan suggests'
    }
    intro = source_phrases.get(source, 'Your scan suggests')
    emotion_label = normalized_emotion if normalized_emotion != 'unknown' else 'an unclear mood'
    mood_actions = {
        'angry': 'Step away from the trigger for a few minutes, relax your shoulders, and write the main thing you need before replying.',
        'anger': 'Step away from the trigger for a few minutes, relax your shoulders, and write the main thing you need before replying.',
        'disgust': 'Give yourself some distance from what feels uncomfortable, rinse your face or drink water, and choose one clean next step.',
        'disgusted': 'Give yourself some distance from what feels uncomfortable, rinse your face or drink water, and choose one clean next step.',
        'fear': 'Ground yourself by naming five things around you, slow your breathing, and handle only the next small task.',
        'fearful': 'Ground yourself by naming five things around you, slow your breathing, and handle only the next small task.',
        'happy': 'Enjoy the good moment and protect it: share it with someone, note what helped, or plan one small follow-up.',
        'joy': 'Enjoy the good moment and protect it: share it with someone, note what helped, or plan one small follow-up.',
        'neutral': 'Use this steady moment to check your energy, stretch briefly, and pick one simple thing that supports the rest of your day.',
        'sad': 'Be gentle with yourself: take a warm drink, send one message to someone safe, and choose a tiny task instead of pushing hard.',
        'sadness': 'Be gentle with yourself: take a warm drink, send one message to someone safe, and choose a tiny task instead of pushing hard.',
        'surprise': 'Pause before reacting, take a slow breath, and give yourself a moment to understand what changed.',
        'surprised': 'Pause before reacting, take a slow breath, and give yourself a moment to understand what changed.'
    }
    action = mood_actions.get(
        normalized_emotion,
        'Take a small reset: drink water, breathe slowly for a minute, and check in with what you need right now.'
    )
    if level == 'high':
        return f"{intro} {emotion_label}. {action} If this feels intense or keeps building, talk to the chatbot, a trusted person, or a doctor."
    return f"{intro} {emotion_label}. {action}"

def build_recommendation_prompt(emotion, severity, source, confidence=None, all_emotions=None, source_results=None):
    severity = severity or {}
    return f"""
You write fresh, mood-aware result-page recommendations for EmoTrack, an emotional wellness app.

Detected result:
Source: {source or 'unknown'}
Mood/emotion: {emotion or 'unknown'}
Confidence: {confidence if confidence is not None else 'unknown'}
Severity level: {severity.get('level') or 'unknown'}
Severity score: {severity.get('score') if severity.get('score') is not None else 'unknown'}
Top emotion distribution: {summarize_emotion_distribution(all_emotions)}
Source breakdown: {summarize_source_results(source_results)}

Write one personalized recommendation for the result page based on this detected mood and severity.
Do not use a fixed template and do not repeat the input labels mechanically.
Do not diagnose or make medical claims.
If severity is high, gently suggest talking to the chatbot, a trusted person, or a doctor.
Keep it practical, warm, and specific. Use simple English. Return only the recommendation text.
Length: 25 to 55 words.
"""

def generate_ai_recommendation(emotion, severity, source, confidence=None, all_emotions=None, source_results=None):
    """Generate a result-page recommendation from Gemini based on the detected mood."""
    if not GEMINI_API_KEY:
        return None, 'unavailable'

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_RECOMMENDATION_MODEL}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": build_recommendation_prompt(
                                emotion,
                                severity,
                                source,
                                confidence,
                                all_emotions,
                                source_results
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": 120,
                "temperature": 0.9,
                "topP": 0.95,
                "thinkingConfig": {
                    "thinkingBudget": 0
                }
            }
        }
        response = _gemini_session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=float(os.getenv("GEMINI_RECOMMENDATION_TIMEOUT", "12")),
            proxies={"http": None, "https": None}
        )
        if response.status_code != 200:
            print(f"Gemini recommendation error {response.status_code}: {response.text[:200]}")
            return None, 'unavailable'

        data = response.json()
        candidates = data.get('candidates') or []
        if not candidates:
            return None, 'unavailable'

        parts = candidates[0].get('content', {}).get('parts', [])
        text = clean_ai_recommendation(''.join(part.get('text', '') for part in parts if part.get('text')))
        word_count = len(text.split())
        if word_count < 8 or word_count > 80:
            return None, 'unavailable'
        return text, 'ai'
    except requests.exceptions.RequestException as e:
        print(f"Gemini recommendation request error: {e}")
        return None, 'unavailable'
    except Exception as e:
        print(f"Gemini recommendation error: {e}")
        return None, 'unavailable'

def enrich_emotion_result(result, source, include_ai_recommendation=True):
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
        'timestamp': datetime.datetime.now().isoformat(),
        'date': datetime.date.today().isoformat()
    }
    recommendation = fallback_recommendation(result.get('emotion'), severity, source)
    recommendation_source = 'local'
    if include_ai_recommendation:
        ai_recommendation, ai_recommendation_source = generate_ai_recommendation(
            result.get('emotion'),
            severity,
            source,
            result.get('confidence'),
            result.get('all_emotions')
        )
        if ai_recommendation:
            recommendation = ai_recommendation
            recommendation_source = ai_recommendation_source
    enriched['suggestion'] = recommendation
    enriched['recommendation'] = recommendation
    enriched['recommendation_source'] = recommendation_source
    return enriched

def save_emotion_to_firebase_async(request_headers, emotion_type, emotion_data):
    """Persist emotion result without blocking the detection response."""
    header = request_headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return

    token = header.split('Bearer ', 1)[-1].strip()
    if not token:
        return

    emotion_snapshot = dict(emotion_data)

    def worker():
        try:
            decoded = auth.verify_id_token(token)
            save_emotion_to_firebase(decoded['uid'], emotion_type, emotion_snapshot)
        except Exception as e:
            print(f"Firebase async save error: {e}")

    threading.Thread(target=worker, daemon=True).start()

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
            'recommendation': emotion_data.get('recommendation'),
            'recommendation_source': emotion_data.get('recommendation_source'),
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
            'recommendation': severity_data.get('recommendation'),
            'recommendation_source': severity_data.get('recommendation_source'),
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
            return jsonify({"error": "Invalid audio format. Allowed: wav, mp3, ogg, flac, m4a, webm"}), 400
        
        if voice_detector is None:
            return jsonify({"error": "Voice emotion model not loaded"}), 500
        
        tmp_path = None
        try:
            # Save temp file
            filename = secure_filename(file.filename)
            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
                file.save(tmp.name)
                tmp_path = tmp.name

            # Detect emotion; voice response should not wait for external recommendation APIs.
            result = enrich_emotion_result(
                voice_detector.detect_emotion_from_file(tmp_path),
                'voice',
                include_ai_recommendation=False
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        
        # Save to Firebase without delaying the result shown to the user.
        if 'emotion' in result:
            save_emotion_to_firebase_async(request.headers, 'voice', result)
        
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
        
        result = enrich_emotion_result(
            voice_detector.detect_emotion_from_base64(data['audio'], audio_format),
            'voice',
            include_ai_recommendation=False
        )
        
        # Save to Firebase without delaying the result shown to the user.
        if 'emotion' in result:
            save_emotion_to_firebase_async(request.headers, 'voice', result)
        
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
        recommendation, recommendation_source = generate_ai_recommendation(
            severity_result.get('emotion'),
            severity_result.get('severity'),
            'multimodal',
            severity_result.get('confidence'),
            severity_result.get('all_emotions'),
            severity_result.get('source_results')
        )
        if not recommendation:
            recommendation = fallback_recommendation(
                severity_result.get('emotion'),
                severity_result.get('severity'),
                'multimodal'
            )
            recommendation_source = 'local'
        severity_result['suggestion'] = recommendation
        severity_result['recommendation'] = recommendation
        severity_result['recommendation_source'] = recommendation_source
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
        },
        "voice_engine": VOICE_ENGINE
    }), 200
