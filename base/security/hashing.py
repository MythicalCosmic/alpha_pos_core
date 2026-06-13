from django.contrib.auth.hashers import make_password, check_password

_DUMMY_HASH = None


def hash_password(password):
    return make_password(password)


def verify_password(password, hashed):
    return check_password(password, hashed)


def verify_password_dummy(password):
    """Run a dummy verify so the user-not-found branch of login takes the
    same wall-clock time as the wrong-password branch. Always returns False.

    Without this, an attacker can probe email enumeration with a stopwatch:
    unknown emails return in microseconds, real emails spend tens of ms in
    bcrypt/PBKDF2.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password('dummy-hash-for-timing-equalization')
    check_password(password, _DUMMY_HASH)
    return False
