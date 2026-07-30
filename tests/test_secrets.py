import pytest
from cryptography.fernet import Fernet
from vts.core.secrets import encrypt_secrets, decrypt_secrets, SecretsKeyMissing, load_secrets_key


def test_encrypt_decrypt_roundtrip():
    key = Fernet.generate_key().decode()
    data = {"api_token": "s3cr3t", "other": "value"}
    blob = encrypt_secrets(data, key)
    assert isinstance(blob, (bytes, bytearray))
    assert b"s3cr3t" not in blob  # ciphertext, not plaintext
    assert decrypt_secrets(blob, key) == data


def test_empty_dict_roundtrip():
    key = Fernet.generate_key().decode()
    assert decrypt_secrets(encrypt_secrets({}, key), key) == {}


def test_load_secrets_key_missing_raises():
    class S:
        secrets_key = ""
    with pytest.raises(SecretsKeyMissing):
        load_secrets_key(S())
