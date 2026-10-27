import secrets

# No 0/O, 1/l/I: temporary passwords get read out over the phone and typed from emails.
_ALPHABET = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'


def generate_temporary_password(groups=3, size=4):
    """e.g. 'Kf7m-Rp3x-Wq9n': 12 random characters (about 68 bits), in dash-separated groups."""
    return '-'.join(''.join(secrets.choice(_ALPHABET) for _ in range(size)) for _ in range(groups))
