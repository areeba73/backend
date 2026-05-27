import numpy as np
import cv2
import tensorflow as tf
from collections import deque, Counter

# Load pre-trained model
model = tf.keras.models.load_model('emotion_detection_model.h5')

# Emotion dictionary
emotion_dict = {0: "Angry", 1: "Disgusted", 2: "Fearful", 3: "Happy", 
                4: "Neutral", 5: "Sad", 6: "Surprised"}

# Mood suggestions
mood_suggestions = {
    "Happy": "Keep smiling! 😄",
    "Sad": "Feeling sad? Try listening to your favorite song 🎵",
    "Angry": "Feeling angry? Take 5 deep breaths 😌",
    "Fearful": "Feeling scared? Stay calm and breathe 🌿",
    "Disgusted": "Feeling irritated? Relax for a moment 🧘",
    "Surprised": "You look surprised! Stay curious 🤔",
    "Neutral": "Stay balanced and positive 🙂"
}

# Webcam capture
cap = cv2.VideoCapture(0)

# Smoothing parameters
frame_window = 10
emotion_window = deque(maxlen=frame_window)

while True:
    ret, frame = cap.read()
    if not ret:
        break

    facecasc = cv2.CascadeClassifier('haarcascade_frontalface_default.xml')
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = facecasc.detectMultiScale(gray, scaleFactor=1.3, minNeighbors=5)

    for (x, y, w, h) in faces:
        cv2.rectangle(frame, (x, y-50), (x+w, y+h+10), (255, 0, 0), 2)
        roi_gray = gray[y:y + h, x:x + w]
        cropped_img = np.expand_dims(np.expand_dims(cv2.resize(roi_gray, (48, 48)), -1), 0)

        # Prediction
        prediction = model.predict(cropped_img)
        maxindex = int(np.argmax(prediction))
        confidence = np.max(prediction)

        # Only add high-confidence predictions
        if confidence > 0.6:
            emotion_window.append(emotion_dict[maxindex])

        # Smooth emotion: most frequent in last N frames
        if len(emotion_window) > 0:
            emotion_label_smooth = Counter(emotion_window).most_common(1)[0][0]
        else:
            emotion_label_smooth = emotion_dict[maxindex]

        # Display emotion
        cv2.putText(frame, emotion_label_smooth, (x+20, y-60), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, cv2.LINE_AA)

        # Display mood suggestion
        suggestion = mood_suggestions.get(emotion_label_smooth, "")
        cv2.putText(frame, suggestion, (x+20, y-20), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA)

    # Show video
    cv2.imshow('Emotion Detector', cv2.resize(frame,(1600,960), interpolation=cv2.INTER_CUBIC))

    # Press 'q' to exit
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
