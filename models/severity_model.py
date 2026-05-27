import math


class SeverityEstimator:
    """Lightweight AI-style severity estimator from model probabilities.

    This is not a clinical diagnosis model. It converts the full emotion
    probability distribution into a stress/severity score using learned-style
    valence/arousal features instead of fixed confidence thresholds.
    """

    def __init__(self):
        self.emotion_weights = {
            "angry": 0.90,
            "anger": 0.90,
            "fear": 0.95,
            "fearful": 0.95,
            "sad": 0.82,
            "sadness": 0.82,
            "disgust": 0.78,
            "disgusted": 0.78,
            "surprise": 0.42,
            "neutral": 0.24,
            "happy": 0.08,
            "joy": 0.08,
        }
        self.source_bias = {
            "face": 0.02,
            "voice": 0.05,
            "text": 0.03,
            "multimodal": 0.08,
        }
        self.fusion_weights = {
            "face": 0.30,
            "voice": 0.35,
            "text": 0.35,
        }

    def _sigmoid(self, value):
        return 1 / (1 + math.exp(-value))

    def predict(self, emotion, confidence, all_emotions=None, source=None):
        emotion = (emotion or "").lower()
        confidence = float(confidence or 0)
        all_emotions = all_emotions or {emotion: confidence}

        weighted_risk = 0.0
        total_probability = 0.0
        for label, probability in all_emotions.items():
            label = (label or "").lower()
            probability = float(probability or 0)
            total_probability += probability
            weighted_risk += probability * self.emotion_weights.get(label, 0.35)

        if total_probability > 0:
            weighted_risk /= total_probability

        confidence_signal = min(max(confidence, 0.0), 1.0)
        dominant_weight = self.emotion_weights.get(emotion, 0.35)
        uncertainty = 1.0 - confidence_signal
        source_adjustment = self.source_bias.get((source or "").lower(), 0.0)

        # Smooth probabilistic score: emotion distribution matters more than
        # a single hard confidence threshold, while uncertainty moderates it.
        raw_score = (
            2.35 * weighted_risk
            + 1.15 * dominant_weight * confidence_signal
            - 0.65 * uncertainty
            + source_adjustment
            - 1.45
        )
        score = round(self._sigmoid(raw_score) * 100)

        if score >= 67:
            level = "High"
        elif score >= 38:
            level = "Medium"
        else:
            level = "Low"

        return {
            "level": level,
            "score": int(score),
            "method": "ai_emotion_distribution",
        }

    def predict_multimodal(self, results):
        fused_emotions = {}
        confidence_sum = 0.0
        used_sources = []

        for source, result in (results or {}).items():
            if not result or "emotion" not in result:
                continue

            source = source.lower()
            source_weight = self.fusion_weights.get(source, 0.0)
            if source_weight <= 0:
                continue

            used_sources.append(source)
            confidence_sum += float(result.get("confidence") or 0) * source_weight
            all_emotions = result.get("all_emotions") or {
                result.get("emotion"): result.get("confidence")
            }

            for emotion, probability in all_emotions.items():
                emotion = (emotion or "").lower()
                probability = float(probability or 0)
                fused_emotions[emotion] = fused_emotions.get(emotion, 0.0) + probability * source_weight

        total = sum(fused_emotions.values())
        if total > 0:
            fused_emotions = {
                emotion: round(probability / total, 4)
                for emotion, probability in fused_emotions.items()
            }

        dominant_emotion = max(fused_emotions, key=fused_emotions.get) if fused_emotions else "neutral"
        confidence = fused_emotions.get(dominant_emotion, confidence_sum)
        severity = self.predict(dominant_emotion, confidence, fused_emotions, "multimodal")
        severity["method"] = "multimodal_emotion_fusion"
        severity["sources"] = used_sources

        return {
            "type": "multimodal",
            "emotion": dominant_emotion,
            "confidence": round(float(confidence), 4),
            "all_emotions": fused_emotions,
            "severity": severity,
        }


severity_estimator = SeverityEstimator()
