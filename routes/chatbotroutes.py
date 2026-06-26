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
You are EmoBot, a real conversational emotional-support chatbot inside a mood tracking app.
Talk to the user naturally and help them calm down according to their latest detected mood and severity.

Use the mood context quietly. Do not give one fixed template for a mood. Do not repeat the same response.
If severity is high, first help the user settle their body with a gentle grounding or breathing suggestion, then continue the conversation.
Validate feelings and speak warmly. Give a complete reply before asking any follow-up question.
Do not shorten, summarize, or cut off your answer. Avoid tiny one-line replies unless the user clearly asks for a very short answer.
When the user needs support, use 2 to 5 natural short paragraphs so the response feels complete but still conversational.
Do not diagnose, do not make medical claims, and do not sound robotic.
If the user may be unsafe or overwhelmed, gently suggest contacting a trusted person, doctor, or emergency support.
Reply in the same style as the user: Roman Urdu for Roman Urdu, English for English.
Do not use Hindi/Devanagari script.
"""
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

def get_recent_chat_context(user_id, limit=8):
    """Get recent chat turns so Gemini can reply like an ongoing conversation."""
    try:
        if not user_id:
            return []

        docs = (db.collection('users')
                .document(user_id)
                .collection('chats')
                .order_by('timestamp', direction='DESCENDING')
                .limit(limit)
                .stream())

        history = [doc.to_dict() for doc in docs]
        history.reverse()
        return history
    except Exception as e:
        print(f"Chat context fetch error: {e}")
        return []

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

def build_chat_prompt(message, emotion_context=None, chat_history=None, retry_note=None):
    context_lines = []
    if emotion_context:
        context_lines = [
            "Latest detected mood context from the app:",
            f"Mood/emotion: {emotion_context.get('emotion') or 'unknown'}",
            f"Severity: {emotion_context.get('severity_level') or 'unknown'}",
            f"App suggestion: {emotion_context.get('suggestion') or 'none'}",
            "",
            "Use this as context, but speak naturally. Do not just repeat these labels."
        ]
    else:
        context_lines = [
            "No latest mood scan is available. Respond supportively based on the user's message only."
        ]

    history_lines = []
    for chat in chat_history or []:
        user_text = str(chat.get('user_message') or '')
        bot_text = str(chat.get('bot_response') or '')
        if user_text:
            history_lines.append(f"User: {user_text}")
        if bot_text:
            history_lines.append(f"EmoBot: {bot_text}")

    retry_lines = []
    if retry_note:
        retry_lines = ["", retry_note]

    return (
        f"{SYSTEM_PROMPT}\n\n"
        + "\n".join(context_lines)
        + ("\n\nRecent conversation:\n" + "\n".join(history_lines) if history_lines else "")
        + "\n\nCurrent user message:\n"
        + message
        + "\n\nReply as EmoBot now with a complete, uncut response."
        + "\n".join(retry_lines)
    )

def is_broken_ai_response(text):
    cleaned = re.sub(r'\s+', ' ', str(text or ''))
    meaningful_chars = re.sub(r'[^A-Za-z0-9]', '', cleaned)
    return len(meaningful_chars) < 8 or len(cleaned.split()) < 2

def clean_bot_response(text, emotion_context=None):
    """Return Gemini's response without shortening or compacting it."""
    return str(text or '')

def call_gemini_rest_api(message, emotion_context=None, chat_history=None):
    """Call Gemini API using REST endpoint"""
    try:
        url = f"https://generativelanguage.googleapis.com/v1/models/{available_model}:generateContent?key={GEMINI_API_KEY}"
        
        headers = {
            "Content-Type": "application/json"
        }

        retry_note = None
        for attempt in range(2):
            payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": build_chat_prompt(message, emotion_context, chat_history, retry_note)
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "maxOutputTokens": 2048,
                    "temperature": 0.85,
                    "topP": 0.95
                }
            }
            
            print(f"Calling Gemini API with model: {available_model}, attempt {attempt + 1}...")
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            
            print(f"Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print("Response received")
                
                if 'candidates' in data and len(data['candidates']) > 0:
                    candidate = data['candidates'][0]
                    parts = candidate.get('content', {}).get('parts', [])
                    text = ''.join(part.get('text', '') for part in parts if part.get('text'))
                    if text and not is_broken_ai_response(text):
                        print(f"Text: {text}")
                        return clean_bot_response(text, emotion_context)

                    print(f"Gemini returned broken/no text. finishReason={candidate.get('finishReason')} text={text if text else ''}")
                    retry_note = "Your previous response was empty or too short. Give a complete, natural, calming reply now. Do not make it a tiny one-line answer."
                    continue

                retry_note = "No valid response was produced. Give a complete, natural, calming reply now. Do not make it a tiny one-line answer."
                continue

            break

        if response.status_code != 200:
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
                    print(f"Gemini API error: {msg[:200]}")
            except:
                pass
            if response.status_code == 429 or 'quota' in error_text.lower():
                return QUOTA_LIMIT_MESSAGE
            if response.status_code in [500, 503] or 'high demand' in error_text.lower() or 'overloaded' in error_text.lower():
                return MODEL_BUSY_MESSAGE
            return MODEL_BUSY_MESSAGE

        return MODEL_BUSY_MESSAGE
    
    except requests.exceptions.Timeout:
        print(" Request timed out")
        return MODEL_BUSY_MESSAGE
    except Exception as e:
        print(f" Error: {e}")
        import traceback
        traceback.print_exc()
        return MODEL_BUSY_MESSAGE

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
        
        user_message = data.get('message', '')
        
        if not user_message:
            return jsonify({"error": "Message cannot be empty"}), 400
        
        # Get user ID (optional)
        user_id = get_user_from_token(request)
        emotion_context = get_latest_emotion_context(user_id)
        chat_history = get_recent_chat_context(user_id)
        
        print(f"\nUser: {user_message}")
        
        # Call Gemini API
        bot_response = call_gemini_rest_api(user_message, emotion_context, chat_history)
        
        print(f"Bot: {bot_response}\n")
        
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
