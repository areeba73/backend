import numpy as np
import soundfile as sf
from scipy import signal
import os
from math import gcd

VOICE_MAX_ANALYSIS_SECONDS = float(os.getenv("VOICE_MAX_ANALYSIS_SECONDS", "7"))
VOICE_ENGINE = "soundfile_numpy_v2"
NO_SPEECH_ERROR = "No clear voice detected. Please speak while recording and try again."
MIN_VOICE_RMS = float(os.getenv("MIN_VOICE_RMS", "0.0025"))
MIN_VOICE_PEAK = float(os.getenv("MIN_VOICE_PEAK", "0.012"))
MIN_ACTIVE_VOICE_RATIO = float(os.getenv("MIN_ACTIVE_VOICE_RATIO", "0.05"))
MIN_ACTIVE_VOICE_SECONDS = float(os.getenv("MIN_ACTIVE_VOICE_SECONDS", "0.25"))
MIN_DOMINANT_CONFIDENCE = float(os.getenv("MIN_DOMINANT_CONFIDENCE", "0.16"))

class VoiceEmotionDetector:
    def __init__(self):
        """Initialize voice emotion detector with pretrained models"""
        self.model = None
        self.torch = None
        self.use_transformer = os.getenv("VOICE_USE_TRANSFORMER", "false").lower() == "true"

        if self.use_transformer:
            try:
                import torch
                from transformers import pipeline

                self.torch = torch
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
                print("Falling back to fast voice feature detector.")
                self.model_loaded = True
                self.use_transformer = False
        else:
            self.model_loaded = True
            print(f"Using fast voice feature detector ({VOICE_ENGINE}).")

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

    def load_audio_file(self, audio_path, target_sample_rate=16000):
        """Load audio quickly without routing WAV files through librosa's decoder."""
        try:
            audio_array, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
            if getattr(audio_array, "ndim", 1) > 1:
                audio_array = np.mean(audio_array, axis=1)

            audio_array = np.nan_to_num(np.asarray(audio_array, dtype=np.float32))
            if sample_rate != target_sample_rate and len(audio_array):
                divisor = gcd(int(sample_rate), int(target_sample_rate))
                audio_array = signal.resample_poly(
                    audio_array,
                    target_sample_rate // divisor,
                    sample_rate // divisor
                ).astype(np.float32)
                sample_rate = target_sample_rate

            return audio_array, sample_rate
        except Exception as soundfile_error:
            print(f"soundfile audio load failed, trying librosa: {soundfile_error}")
            import librosa
            return librosa.load(audio_path, sr=target_sample_rate, mono=True)

    def frame_audio(self, audio_array, frame_length=2048, hop_length=512):
        """Split audio into fixed frames without relying on librosa/numba."""
        if len(audio_array) < frame_length:
            padded = np.pad(audio_array, (0, frame_length - len(audio_array)))
            return padded.reshape(1, frame_length)

        starts = range(0, len(audio_array) - frame_length + 1, hop_length)
        return np.asarray([audio_array[start:start + frame_length] for start in starts], dtype=np.float32)

    def calculate_frame_rms(self, audio_array, frame_length=2048, hop_length=512):
        frames = self.frame_audio(audio_array, frame_length, hop_length)
        return np.sqrt(np.mean(np.square(frames), axis=1))

    def calculate_zero_crossing_rate(self, audio_array):
        if len(audio_array) < 2:
            return 0.0
        signs = np.signbit(audio_array)
        return float(np.mean(signs[1:] != signs[:-1]))

    def calculate_spectral_features(self, audio_array, sample_rate, frame_length=2048, hop_length=512):
        frames = self.frame_audio(audio_array, frame_length, hop_length)
        window = np.hanning(frame_length).astype(np.float32)
        spectrum = np.abs(np.fft.rfft(frames * window, axis=1))
        freqs = np.fft.rfftfreq(frame_length, d=1.0 / float(sample_rate))
        magnitudes = spectrum + 1e-10
        magnitude_sums = np.sum(magnitudes, axis=1)

        centroids = np.sum(magnitudes * freqs, axis=1) / magnitude_sums
        cumulative = np.cumsum(magnitudes, axis=1)
        rolloff_targets = 0.85 * magnitude_sums
        rolloff_indices = np.argmax(cumulative >= rolloff_targets[:, None], axis=1)
        rolloffs = freqs[rolloff_indices]

        return float(np.mean(centroids)), float(np.mean(rolloffs))

    def validate_voice_activity(self, audio_array, sample_rate):
        """Reject silence and background noise before emotion classification."""
        if audio_array is None or len(audio_array) == 0:
            return False, {"error": "No audio data found"}

        audio_array = np.nan_to_num(audio_array.astype(np.float32))
        duration = len(audio_array) / float(sample_rate or 16000)
        peak = float(np.max(np.abs(audio_array)))
        rms = float(np.sqrt(np.mean(np.square(audio_array))))

        frame_rms = self.calculate_frame_rms(audio_array, frame_length=2048, hop_length=512)
        noise_floor = float(np.percentile(frame_rms, 20)) if len(frame_rms) else 0.0
        # If the user speaks through most of the clip, the 20th percentile is
        # still voice rather than room noise. Cap the adaptive threshold so
        # continuous clear speech is not rejected as inactive.
        active_threshold = max(MIN_VOICE_RMS, min(noise_floor * 1.8, rms * 0.45))
        active_ratio = float(np.mean(frame_rms > active_threshold)) if len(frame_rms) else 0.0
        active_seconds = duration * active_ratio

        quality = {
            "duration": round(duration, 2),
            "rms": round(rms, 5),
            "peak": round(peak, 5),
            "active_ratio": round(active_ratio, 3),
            "active_seconds": round(active_seconds, 2),
            "noise_floor": round(noise_floor, 5)
        }

        if duration < 0.5:
            return False, {"error": NO_SPEECH_ERROR, "reason": "too_short", "audio_quality": quality}

        if peak < MIN_VOICE_PEAK and rms < MIN_VOICE_RMS:
            return False, {"error": NO_SPEECH_ERROR, "reason": "too_quiet", "audio_quality": quality}

        if active_ratio < MIN_ACTIVE_VOICE_RATIO or active_seconds < MIN_ACTIVE_VOICE_SECONDS:
            return False, {"error": NO_SPEECH_ERROR, "reason": "not_enough_voice", "audio_quality": quality}

        return True, quality

    def detect_emotion_from_features(self, audio_array, sample_rate, voice_quality=None):
        """Fast fallback detector based on voice energy and tone features."""
        if audio_array is None or len(audio_array) == 0:
            return {"error": "No audio data found"}

        audio_array = np.nan_to_num(audio_array.astype(np.float32))
        rms = float(np.sqrt(np.mean(np.square(audio_array))))

        zcr = self.calculate_zero_crossing_rate(audio_array)
        centroid, rolloff = self.calculate_spectral_features(audio_array, sample_rate)

        energy = min(rms / 0.08, 1.0)
        brightness = min(centroid / 3500.0, 1.0)
        sharpness = min(zcr / 0.18, 1.0)
        warmth = max(0.0, 1.0 - brightness)

        # Each emotion gets a different acoustic profile so the fast detector
        # does not collapse into one repeated mood.
        emotion_profiles = {
            "neutral": {"energy": 0.35, "brightness": 0.45, "sharpness": 0.25, "weight": 1.00},
            "happy": {"energy": 0.72, "brightness": 0.68, "sharpness": 0.35, "weight": 1.08},
            "angry": {"energy": 0.82, "brightness": 0.55, "sharpness": 0.78, "weight": 1.08},
            "sad": {"energy": 0.18, "brightness": 0.25, "sharpness": 0.16, "weight": 0.95},
            "fear": {"energy": 0.50, "brightness": 0.78, "sharpness": 0.72, "weight": 1.04},
            "surprise": {"energy": 0.64, "brightness": 0.84, "sharpness": 0.45, "weight": 1.06},
            "disgust": {"energy": 0.32, "brightness": 0.34, "sharpness": 0.66, "weight": 1.02},
        }

        scores = {}
        for emotion, profile in emotion_profiles.items():
            distance = (
                (energy - profile["energy"]) ** 2 +
                (brightness - profile["brightness"]) ** 2 +
                (sharpness - profile["sharpness"]) ** 2
            )
            scores[emotion] = profile["weight"] * float(np.exp(-5.0 * distance))

        total = sum(scores.values()) or 1.0
        all_emotions = {
            emotion: round(score / total, 4)
            for emotion, score in scores.items()
        }
        dominant_emotion = max(all_emotions, key=all_emotions.get)
        confidence = all_emotions[dominant_emotion]

        print(
            "Voice Features: "
            f"emotion={dominant_emotion}, confidence={confidence:.4f}, rms={rms:.5f}, "
            f"zcr={zcr:.5f}, centroid={centroid:.2f}, rolloff={rolloff:.2f}"
        )

        if confidence < MIN_DOMINANT_CONFIDENCE:
            dominant_emotion = "neutral"
            confidence = max(confidence, MIN_DOMINANT_CONFIDENCE)
            all_emotions[dominant_emotion] = max(all_emotions.get(dominant_emotion, 0.0), round(confidence, 4))

        return {
            "emotion": dominant_emotion,
            "confidence": confidence,
            "all_emotions": all_emotions
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

            audio_array, sample_rate = self.load_audio_file(audio_path, target_sample_rate=16000)
            has_voice, voice_quality = self.validate_voice_activity(audio_array, sample_rate)
            if not has_voice:
                print(f"Voice rejected: {voice_quality}")
                return voice_quality

            if not self.use_transformer or self.model is None:
                result = self.detect_emotion_from_features(audio_array, sample_rate, voice_quality)
                if "emotion" in result:
                    result["audio_quality"] = voice_quality
                return result

            # Predict using transformer model without requiring ffmpeg
            max_samples = int(sample_rate * VOICE_MAX_ANALYSIS_SECONDS)
            transformer_audio = audio_array[:max_samples] if max_samples > 0 else audio_array
            with self.torch.inference_mode():
                predictions = self.model(transformer_audio, top_k=5)

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

            if confidence < MIN_DOMINANT_CONFIDENCE:
                dominant_emotion = "neutral"
                confidence = max(confidence, MIN_DOMINANT_CONFIDENCE)
                result[dominant_emotion] = max(result.get(dominant_emotion, 0.0), round(confidence, 4))

            print(f"Voice Emotion: {dominant_emotion} ({confidence})")

            return {
                "emotion": dominant_emotion,
                "confidence": confidence,
                "all_emotions": result,
                "audio_quality": voice_quality
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
