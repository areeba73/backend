import librosa
import numpy as np
import torch
from transformers import pipeline
import soundfile as sf
from scipy import signal
import os

class VoiceEmotionDetector:
    def __init__(self):
        """Initialize voice emotion detector with pretrained models"""
        try:
            print("Loading voice emotion model from HuggingFace...")
            print("   (First download can take a few minutes; future use cache)")
            
            # HuggingFace speech emotion model with categorical labels
            self.model = pipeline(
                "audio-classification",
                model="superb/wav2vec2-base-superb-er",
                device=0 if torch.cuda.is_available() else -1
            )
            self.model_loaded = True
            print("Voice emotion model loaded successfully!")
        except Exception as e:
            print(f"Error loading voice model: {e}")
            self.model_loaded = False
        
        # Emotion mapping
        self.emotion_map = {
            'anger': 'angry',
            'ang': 'angry',
            'disgust': 'disgust',
            'fear': 'fear',
            'happiness': 'happy',
            'happy': 'happy',
            'hap': 'happy',
            'neutral': 'neutral',
            'neu': 'neutral',
            'sadness': 'sad',
            'sad': 'sad',
            'surprise': 'surprise'
        }
    
    def detect_emotion_from_file(self, audio_path):
        """
        Detect emotion from audio file
        Supports: .wav, .mp3, .ogg, .flac
        """
        if not self.model_loaded:
            return {"error": "Voice model not loaded"}
        
        try:
            if not os.path.exists(audio_path):
                return {"error": "Audio file not found"}
            
            print(f"Analyzing voice: {audio_path}")
            
            audio_array, sample_rate = librosa.load(audio_path, sr=16000, mono=True)

            # Predict using transformer model without requiring ffmpeg
            predictions = self.model(audio_array, top_k=None)
            
            # Parse predictions
            result = {}
            for pred in predictions:
                label = pred.get('label', '').lower()
                score = pred.get('score', 0)
                
                # Map to emotion
                emotion_name = self.emotion_map.get(label, label)
                result[emotion_name] = round(float(score), 4)
            
            # Get dominant emotion
            dominant_emotion = max(result, key=result.get)
            confidence = result[dominant_emotion]
            
            print(f"Voice Emotion: {dominant_emotion} ({confidence})")
            
            return {
                "emotion": dominant_emotion,
                "confidence": confidence,
                "all_emotions": result
            }
        
        except Exception as e:
            return {"error": str(e)}
    
    def detect_emotion_from_base64(self, base64_audio, audio_format='wav'):
        """
        Detect emotion from base64 encoded audio
        """
        import base64
        import tempfile
        
        if not self.model_loaded:
            return {"error": "Voice model not loaded"}
        
        try:
            # Decode base64
            audio_data = base64.b64decode(base64_audio.split(',')[1] if ',' in base64_audio else base64_audio)
            
            # Save to temp file
            with tempfile.NamedTemporaryFile(suffix=f'.{audio_format}', delete=False) as tmp_file:
                tmp_file.write(audio_data)
                tmp_path = tmp_file.name
            
            # Process
            result = self.detect_emotion_from_file(tmp_path)
            
            # Clean up
            os.remove(tmp_path)
            
            return result
        
        except Exception as e:
            return {"error": str(e)}

# Initialize global detector
try:
    voice_detector = VoiceEmotionDetector()
except Exception as e:
    print(f"Error initializing voice detector: {e}")
    voice_detector = None
