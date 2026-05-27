from transformers import pipeline
import torch

class TextEmotionDetector:
    def __init__(self):
        """Initialize text emotion detector with pretrained model"""
        try:
            print("Loading text emotion model from HuggingFace...")
            print("   (First download: ~2-3 min, ~250MB - future use cache)")
            
            # Using distilbert model fine-tuned on emotion dataset
            self.model = pipeline(
                "text-classification",
                model="j-hartmann/emotion-english-distilroberta-base",
                device=0 if torch.cuda.is_available() else -1
            )
            self.model_loaded = True
            print("Text emotion model loaded successfully!")
        except Exception as e:
            print(f"Error loading text model: {e}")
            self.model_loaded = False
        
        # Emotion mapping
        self.emotion_map = {
            'anger': 'anger',
            'disgust': 'disgust',
            'fear': 'fear',
            'joy': 'joy',
            'neutral': 'neutral',
            'sadness': 'sadness',
            'surprise': 'surprise'
        }
    
    def detect_emotion(self, text):
        """
        Detect emotion from text input
        Returns: dict with emotion and confidence
        """
        if not self.model_loaded:
            return {"error": "Text model not loaded"}
        
        try:
            if not text or len(text.strip()) == 0:
                return {"error": "Text cannot be empty"}
            
            # Limit text length for better performance
            text = text[:512]
            
            print(f"Analyzing text: {text[:50]}...")
            
            # Predict
            predictions = self.model(text, top_k=None)
            
            # Parse predictions
            result = {}
            for pred in predictions:
                label = pred.get('label', '').lower()
                score = pred.get('score', 0)
                
                # Map label to emotion name
                emotion_name = self.emotion_map.get(label, label)
                result[emotion_name] = round(float(score), 4)
            
            # Get dominant emotion
            dominant_emotion = max(result, key=result.get)
            confidence = result[dominant_emotion]
            
            print(f"Text Emotion: {dominant_emotion} ({confidence})")
            
            return {
                "emotion": dominant_emotion,
                "confidence": confidence,
                "all_emotions": result
            }
        
        except Exception as e:
            return {"error": str(e)}
    
    def detect_emotion_batch(self, texts):
        """
        Detect emotions from multiple texts
        """
        if not self.model_loaded:
            return {"error": "Text model not loaded"}
        
        try:
            results = []
            for text in texts:
                result = self.detect_emotion(text)
                results.append(result)
            
            return {"results": results}
        
        except Exception as e:
            return {"error": str(e)}

# Initialize global detector
try:
    text_detector = TextEmotionDetector()
except Exception as e:
    print(f"Error initializing text detector: {e}")
    text_detector = None
