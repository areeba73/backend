import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

# Firebase Initialization
cred = credentials.Certificate("serviceAccountKey.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

class Config:
    FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY")
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")