import jwt, os
from datetime import datetime, timedelta

ADMIN_API_PASSWORD = os.environ.get("ADMIN_API_PASSWORD", None)
API_SECRET = os.environ.get("API_SECRET", None)


def _verify_env():
    if not all([ADMIN_API_PASSWORD, API_SECRET]):
        raise Exception("API environment is not setup")


def create_user(password):
    _verify_env()
    if password != ADMIN_API_PASSWORD:
        return None
    expiry = datetime.now() + timedelta(days=30)
    return jwt.encode(
        {"role": "admin", "usage": "He he , its a prank", "exp": expiry},
        API_SECRET,
        algorithm="HS256",
    )


def check_user(encoded_jwt):
    _verify_env()
    decoded = jwt.decode(encoded_jwt, API_SECRET, algorithms=["HS256"])
    expiry = decoded.get("exp")
    if datetime.now().timestamp() >= expiry:
        return None
    return decoded
