from django.contrib.auth.hashers import make_password, check_password

_DUMMY_HASH = None


def hash_password(password):
    return make_password(password)


def verify_password(password, hashed):
    return check_password(password, hashed)


def verify_password_dummy(password):
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password('dummy-hash-for-timing-equalization')
    check_password(password, _DUMMY_HASH)
    return False
