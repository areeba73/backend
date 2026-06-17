from flask import Blueprint, request, jsonify
from config import db
import firebase_admin
from firebase_admin import auth
import datetime
import os
import re
import requests

chatbot_bp = Blueprint('chatbot', __name__)

# Configure Gemini API using REST only (no SDK)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

model_loaded = False
available_model = "gemini-2.5-flash"  # Use the available model from API
QUOTA_LIMIT_MESSAGE = "Daily AI limit reached. Please try again tomorrow."
MODEL_BUSY_MESSAGE = "EmoBot is busy right now. Please try again in a moment."

SYSTEM_PROMPT = """
You are EmoBot, a calm emotional-support companion inside a mood tracking app.
Your job is to help the user feel heard, calmer, and a little less stressed.

Reply rules:
- Keep replies very short: 1 to 2 simple sentences only.
- Keep the full reply under 35 words.
- Use a warm, gentle tone.
- Do not give long explanations unless the user asks.
- Ask only one soft follow-up question.
- Avoid diagnosis, medical claims, or alarming language.
- If the user sounds stressed, suggest one tiny grounding step like slow breathing, relaxing shoulders, or naming one feeling.
- Use the latest detected mood and severity when available. The app detects these moods: angry, disgust, fear, happy, neutral, sad, and surprise.
- Do not wait for the user to tell you their mood. If detected mood context is available, quietly use it from the start.
- Do not sound technical. Do not mention confidence percentages unless the user asks.
- You may naturally acknowledge the detected mood, for example "Lagta hai kuch unexpected hua" for surprise, without saying "your detected mood is surprise".
- For High severity, focus first on calming the user with one immediate grounding step. Gently suggest talking to a trusted person or doctor if they feel unsafe or overwhelmed.
- For Medium severity, validate the feeling and suggest one simple coping exercise.
- For Low severity, keep it light, supportive, and reflective.
- You may recommend one exercise, breathing practice, calming activity, or a search phrase for a calming video when it fits the user's mood.
- Never repeat the same exact response. Generate a fresh reply based on the user's message, detected mood, and severity.
- Reply in simple Roman Urdu or English only.
- Do not use Hindi/Devanagari script. Avoid words like aapka in Hindi style; prefer Pakistani Roman Urdu such as "aap", "theek", "saans", "dil halka".
- If the user writes Roman Urdu, reply in Roman Urdu. If the user writes English, reply in English.
- Never use bullet points, numbered lists, headings, markdown, or long step-by-step answers.
- Your final answer must not contain any Devanagari characters.
- If the user greets you or asks how you are, answer as EmoBot in a warm human-like support tone. Do not say "as an AI" or explain that you do not have feelings.
"""

DEVANAGARI_PATTERN = re.compile(r'[\u0900-\u097F]')
MAX_REPLY_WORDS = 35

if GEMINI_API_KEY:
    print(f"GEMINI_API_KEY found: {GEMINI_API_KEY[:10]}...")
    print(f"Using model: {available_model}")
    model_loaded = True
else:
    print("Warning: GEMINI_API_KEY not set in environment variables")

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

def save_chat_to_firebase(user_id, user_message, bot_response, emotion_context=None):
    """Save chat messages to Firebase"""
    try:
        if not user_id:
            return False
        
        doc_ref = db.collection('users').document(user_id).collection('chats')
        doc_ref.add({
            'user_message': user_message,
            'bot_response': bot_response,
            'emotion_context': emotion_context,
            'timestamp': datetime.datetime.now(),
            'date': datetime.date.today().isoformat()
        })
        return True
    except Exception as e:
        print(f"Firebase chat save error: {e}")
        return False

def serialize_chat_doc(data):
    """Serialize chat document for JSON response"""
    serialized = dict(data)
    timestamp = serialized.get('timestamp')
    if hasattr(timestamp, 'isoformat'):
        serialized['timestamp'] = timestamp.isoformat()
    return serialized

def get_latest_emotion_context(user_id):
    """Get the user's latest emotion/severity result for mood-aware chat."""
    try:
        if not user_id:
            return None

        docs = (db.collection('users')
                .document(user_id)
                .collection('emotions')
                .order_by('timestamp', direction='DESCENDING')
                .limit(5)
                .stream())

        results = [doc.to_dict() for doc in docs]
        if not results:
            return None

        latest = next((item for item in results if item.get('type') == 'multimodal'), results[0])
        severity = latest.get('severity') or {}

        return {
            'source': latest.get('type'),
            'emotion': latest.get('emotion'),
            'confidence': latest.get('confidence'),
            'severity_level': severity.get('level'),
            'severity_score': severity.get('score'),
            'suggestion': latest.get('suggestion')
        }
    except Exception as e:
        print(f"Emotion context fetch error: {e}")
        return None

def build_chat_prompt(message, emotion_context=None):
    context_lines = []
    if emotion_context:
        context_lines = [
            "Latest detected user state from this app:",
            f"- Scan source: {emotion_context.get('source') or 'unknown'}",
            f"- Mood/emotion: {emotion_context.get('emotion') or 'unknown'}",
            f"- Severity: {emotion_context.get('severity_level') or 'unknown'}",
            f"- Severity score: {emotion_context.get('severity_score') or 'unknown'}",
            f"- Existing app suggestion: {emotion_context.get('suggestion') or 'none'}",
            "",
            "Use this state as hidden context from the start. The user should not need to explain their mood again."
        ]
    else:
        context_lines = [
            "No latest mood scan is available. Respond supportively based on the user's message only."
        ]

    return f"{SYSTEM_PROMPT}\n\n" + "\n".join(context_lines) + f"\n\nUser message: {message}"

def build_safe_fallback(emotion_context=None):
    emotion = ((emotion_context or {}).get('emotion') or '').lower()
    severity = ((emotion_context or {}).get('severity_level') or '').lower()

    if severity == 'high':
        return "Aap thora pause lein aur 3 slow saans lein. Yeh surprise achi baat se hua ya tension se?"

    if emotion in ['surprise', 'surprised']:
        return "Lagta hai kuch unexpected hua hai. Yeh surprise positive tha ya stressful?"

    if emotion in ['sad', 'sadness']:
        return "Mujhe lagta hai aap thora heavy feel kar rahe hain. Kis baat ne sab se zyada dil par asar kiya?"

    if emotion in ['angry', 'anger']:
        return "Aap pehle thora pause lein aur shoulders relax karein. Kis cheez ne aap ko trigger kiya?"

    if emotion in ['fear', 'fearful']:
        return "Aap apne around 3 cheezen notice karein jo aap dekh sakte hain. Kis baat ka dar zyada feel ho raha hai?"

    return "Main aap ke sath hoon. Ek slow saans lein, abhi sab se zyada kya feel ho raha hai?"

def clean_bot_response(text, emotion_context=None):
    """Keep Gemini replies short and in Roman Urdu/English for the UI."""
    if not text:
        return "Main aap ke sath hoon. Abhi ek slow saans lein."

    lower_text = text.lower()
    generic_ai_phrases = [
        'as an ai',
        "i don't have feelings",
        'i do not have feelings',
        'physical state like humans',
        'how can i help you today'
    ]

    if DEVANAGARI_PATTERN.search(text) or any(phrase in lower_text for phrase in generic_ai_phrases):
        return build_safe_fallback(emotion_context)

    cleaned = re.sub(r'\s+', ' ', text).strip()
    sentences = re.split(r'(?<=[.!?])\s+', cleaned)
    if len(sentences) > 2:
        cleaned = ' '.join(sentences[:2]).strip()

    words = cleaned.split()

    if len(words) <= MAX_REPLY_WORDS:
        return cleaned

    short = ' '.join(words[:MAX_REPLY_WORDS]).rstrip('.,;:')
    return f"{short}."

def call_gemini_rest_api(message, emotion_context=None):
    """Call Gemini API using REST endpoint"""
    try:
        url = f"https://generativelanguage.googleapis.com/v1/models/{available_model}:generateContent?key={GEMINI_API_KEY}"
        
        headers = {
            "Content-Type": "application/json"
        }
        
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": build_chat_prompt(message, emotion_context)
                        }
                    ]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": 70,
                "temperature": 0.5,
                "topP": 0.9
            }
        }
        
        print(f"Calling Gemini API with model: {available_model}...")
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        print(f"Response Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("Response received")
            
            if 'candidates' in data and len(data['candidates']) > 0:
                candidate = data['candidates'][0]
                if 'content' in candidate and 'parts' in candidate['content']:
                    text = candidate['content']['parts'][0]['text']
                    print(f"Text: {text[:50]}...")
                    return clean_bot_response(text, emotion_context)
            
            return "I couldn't generate a response. Please try again."
        else:
            error_text = response.text
            print(f"API Error {response.status_code}: {error_text[:200]}")
            try:
                error_data = response.json()
                if 'error' in error_data:
                    msg = error_data['error'].get('message', 'Unknown error')
                    status = error_data['error'].get('status', '')
                    if response.status_code == 429 or status == 'RESOURCE_EXHAUSTED' or 'quota' in msg.lower():
                        return QUOTA_LIMIT_MESSAGE
                    if response.status_code in [500, 503] or status in ['UNAVAILABLE', 'ABORTED'] or 'high demand' in msg.lower() or 'overloaded' in msg.lower():
                        return MODEL_BUSY_MESSAGE
                    return f"API Error: {msg[:100]}"
            except:
                pass
            if response.status_code == 429 or 'quota' in error_text.lower():
                return QUOTA_LIMIT_MESSAGE
            if response.status_code in [500, 503] or 'high demand' in error_text.lower() or 'overloaded' in error_text.lower():
                return MODEL_BUSY_MESSAGE
            return f"API Error {response.status_code}"
    
    except requests.exceptions.Timeout:
        print(" Request timed out")
        return "Request timed out. Please try again."
    except Exception as e:
        print(f" Error: {e}")
        import traceback
        traceback.print_exc()
        return f"Error: {str(e)}"

# ============== SEND MESSAGE ==============
@chatbot_bp.route('/chatbot/message', methods=['POST'])
def send_message():
    """
    Send message to Gemini chatbot via REST API
    POST /api/chatbot/message
    JSON: { "message": "How are you?" }
    """
    try:
        if not model_loaded:
            return jsonify({
                "error": "Chatbot not configured",
                "details": "GEMINI_API_KEY not set"
            }), 500
        
        data = request.get_json()
        
        if not data or 'message' not in data:
            return jsonify({"error": "No message provided"}), 400
        
        user_message = data.get('message', '').strip()
        
        if not user_message:
            return jsonify({"error": "Message cannot be empty"}), 400
        
        # Get user ID (optional)
        user_id = get_user_from_token(request)
        emotion_context = get_latest_emotion_context(user_id)
        
        print(f"\nUser: {user_message}")
        
        # Call Gemini API
        bot_response = call_gemini_rest_api(user_message, emotion_context)
        
        print(f"Bot: {bot_response[:50]}...\n")
        
        # Save to Firebase if user logged in
        if user_id:
            save_chat_to_firebase(user_id, user_message, bot_response, emotion_context)
        
        return jsonify({
            "user_message": user_message,
            "bot_response": bot_response,
            "timestamp": datetime.datetime.now().isoformat(),
            "authenticated": user_id is not None,
            "emotion_context": emotion_context
        }), 200
    
    except Exception as e:
        print(f" Error in send_message: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============== GET CHAT HISTORY ==============
@chatbot_bp.route('/chatbot/history', methods=['GET'])
def get_chat_history():
    """
    Get user's chat history
    GET /api/chatbot/history?limit=50
    """
    try:
        user_id = get_user_from_token(request)
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401
        
        limit = request.args.get('limit', 50, type=int)
        
        docs = (db.collection('users')
                .document(user_id)
                .collection('chats')
                .order_by('timestamp', direction='DESCENDING')
                .limit(limit)
                .stream())
        
        history = []
        for doc in docs:
            data = serialize_chat_doc(doc.to_dict())
            data['id'] = doc.id
            history.append(data)
        
        history.reverse()
        
        return jsonify({"history": history, "count": len(history)}), 200
    
    except Exception as e:
        print(f"Error in get_chat_history: {e}")
        return jsonify({"error": str(e)}), 500


# ============== CLEAR CHAT HISTORY ==============
@chatbot_bp.route('/chatbot/history/clear', methods=['DELETE'])
def clear_chat_history():
    """Clear user's chat history"""
    try:
        user_id = get_user_from_token(request)
        if not user_id:
            return jsonify({"error": "Not authenticated"}), 401
        
        docs = (db.collection('users')
                .document(user_id)
                .collection('chats')
                .stream())
        
        count = 0
        for doc in docs:
            doc.reference.delete()
            count += 1
        
        return jsonify({"message": f"Deleted {count} messages"}), 200
    
    except Exception as e:
        print(f"Error in clear_chat_history: {e}")
        return jsonify({"error": str(e)}), 500


# ============== HEALTH CHECK ==============
@chatbot_bp.route('/chatbot/health', methods=['GET'])
def chatbot_health():
    """Check chatbot status"""
    return jsonify({
        "status": "ok" if model_loaded else "error",
        "model": available_model,
        "api_configured": bool(GEMINI_API_KEY),
        "endpoint": "https://generativelanguage.googleapis.com/v1"
    }), 200
