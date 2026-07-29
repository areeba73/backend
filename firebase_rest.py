import requests


_firebase_session = requests.Session()
_firebase_session.trust_env = False
_firebase_session.proxies = {}


def firebase_post(url, *, json=None, timeout=10):
    """Call Firebase REST APIs without inheriting stale local proxy settings."""
    return _firebase_session.post(url, json=json, timeout=timeout, proxies={"http": None, "https": None})
