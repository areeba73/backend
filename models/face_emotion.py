import cv2
import numpy as np
import os
from pathlib import Path

class FaceEmotionDetector:
    def __init__(self):
        # FER2013 folder path - load entire folder
        self.model_folder = "models/pretrained_models/fer2013"
        
        # Check if folder exists
        if not os.path.exists(self.model_folder):
            raise FileNotFoundError(f"FER2013 folder not found at {self.model_folder}")
        
        print(f"Loading FER2013 from folder: {self.model_folder}")
        print(f"Files in folder: {os.listdir(self.model_folder)}")
        
        # Load model files from folder
        self.load_model_from_folder()
        
        # Load face detector
        self.cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        self.face_cascade = cv2.CascadeClassifier(self.cascade_path)
        
        # Emotion labels (FER2013 has 7 emotions)
        self.emotions = ['angry', 'disgust', 'fear', 'happy', 'neutral', 'sad', 'surprise']

    def _detect_faces(self, gray):
        """Detect faces with a strict pass first, then a more tolerant fallback."""
        gray_equalized = cv2.equalizeHist(gray)
        min_size = (max(40, gray.shape[1] // 12), max(40, gray.shape[0] // 12))

        faces = self.face_cascade.detectMultiScale(
            gray_equalized,
            scaleFactor=1.2,
            minNeighbors=5,
            minSize=min_size
        )
        if len(faces) == 0:
            faces = self.face_cascade.detectMultiScale(
                gray_equalized,
                scaleFactor=1.08,
                minNeighbors=3,
                minSize=(30, 30)
            )

        return sorted(faces, key=lambda face: face[2] * face[3], reverse=True)

    def _predict_from_gray_roi(self, roi_gray, face_detected=True):
        roi = cv2.resize(roi_gray, (48, 48))
        roi = roi.astype('float') / 255.0
        roi = np.expand_dims(roi, axis=0)
        roi = np.expand_dims(roi, axis=-1)

        predictions = self.model.predict(roi, verbose=0)
        emotion_idx = np.argmax(predictions[0])
        emotion = self.emotions[emotion_idx]
        confidence = float(predictions[0][emotion_idx])

        return {
            "emotion": emotion,
            "confidence": round(confidence, 4),
            "all_emotions": {
                self.emotions[i]: round(float(predictions[0][i]), 4)
                for i in range(len(self.emotions))
            },
            "face_detected": face_detected
        }

    def _center_face_fallback(self, gray):
        height, width = gray.shape[:2]
        crop_size = int(min(width, height) * 0.65)
        x = max((width - crop_size) // 2, 0)
        y = max((height - crop_size) // 3, 0)
        return gray[y:y + crop_size, x:x + crop_size]
    
    def load_model_from_folder(self):
        """Load model files from FER2013 folder"""
        try:
            # Check for .h5 model file
            h5_files = list(Path(self.model_folder).glob('*.h5'))
            if h5_files:
                model_path = str(h5_files[0])
                print(f"Loading Keras model from: {model_path}")
                from keras.models import load_model
                self.model = load_model(model_path)
                self.model_loaded = True
                return
            
            # Check for PyTorch model
            pth_files = list(Path(self.model_folder).glob('*.pth'))
            if pth_files:
                import torch
                model_path = str(pth_files[0])
                print(f"Loading PyTorch model from: {model_path}")
                self.model = torch.load(model_path)
                self.model.eval()
                self.model_loaded = True
                return
            
            # Check for JSON + weights (custom architecture)
            json_files = list(Path(self.model_folder).glob('*.json'))
            if json_files:
                from keras.models import model_from_json
                json_file = str(json_files[0])
                weights_files = list(Path(self.model_folder).glob('*.h5'))
                
                if weights_files:
                    weights_file = str(weights_files[0])
                    print(f"Loading model from {json_file} and {weights_file}")
                    
                    with open(json_file, 'r') as jf:
                        loaded_model_json = jf.read()
                    
                    self.model = model_from_json(loaded_model_json)
                    self.model.load_weights(weights_file)
                    self.model_loaded = True
                    return
            
            raise FileNotFoundError(f"No model files (.h5, .pth, .json) found in {self.model_folder}")
        
        except Exception as e:
            print(f"Error loading model: {e}")
            self.model_loaded = False
            raise
    
    def detect_emotion_from_image(self, image_path):
        """
        Detect emotion from image file
        Returns: dict with emotion and confidence
        """
        try:
            if not self.model_loaded:
                return {"error": "Model not loaded"}
            
            # Read image
            img = cv2.imread(image_path)
            if img is None:
                return {"error": "Could not read image"}
            
            # Convert to grayscale
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Detect faces
            faces = self._detect_faces(gray)
            
            if len(faces) == 0:
                roi_gray = self._center_face_fallback(gray)
                return self._predict_from_gray_roi(roi_gray, face_detected=False)
            
            # Get first face
            (x, y, w, h) = faces[0]
            roi_gray = gray[y:y+h, x:x+w]
            return self._predict_from_gray_roi(roi_gray)
        
        except Exception as e:
            return {"error": str(e)}
    
    def detect_emotion_from_base64(self, base64_image):
        """
        Detect emotion from base64 encoded image
        """
        import base64
        from io import BytesIO
        from PIL import Image
        
        try:
            if not self.model_loaded:
                return {"error": "Model not loaded"}
            
            # Decode base64
            image_data = base64.b64decode(base64_image.split(',')[1] if ',' in base64_image else base64_image)
            image = Image.open(BytesIO(image_data))
            
            # Convert to OpenCV format
            img_array = np.array(image)
            if len(img_array.shape) == 3 and img_array.shape[2] == 3:
                img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
            else:
                img = img_array
            
            # Same detection logic
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
            faces = self._detect_faces(gray)
            
            if len(faces) == 0:
                roi_gray = self._center_face_fallback(gray)
                return self._predict_from_gray_roi(roi_gray, face_detected=False)
            
            (x, y, w, h) = faces[0]
            roi_gray = gray[y:y+h, x:x+w]
            return self._predict_from_gray_roi(roi_gray)
        
        except Exception as e:
            return {"error": str(e)}

# Initialize global detector
try:
    face_detector = FaceEmotionDetector()
    print("FER2013 Face Emotion Detector loaded successfully!")
except Exception as e:
    print(f"Error loading FER2013: {e}")
    face_detector = None
