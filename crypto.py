"""WebDAV 云端敏感数据加密/解密。

用用户提供的“加密口令”经 scrypt 派生密钥，再用 AES-256-GCM 加密。
口令只在本地配置（webdav_encrypt_pass），上传到云端的是密文，云端泄露也读不到原文。
密文格式: b'SC1' + salt(16) + nonce(12) + ciphertext
"""
import os
import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_MAGIC = b'SC1'
_N = 2 ** 14


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_N, r=8, p=1).derive(passphrase.encode('utf-8'))


def encrypt_bytes(plain: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ct = AESGCM(_derive_key(passphrase, salt)).encrypt(nonce, plain, None)
    return _MAGIC + salt + nonce + ct


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(_MAGIC) or len(blob) < 3 + 16 + 12 + 16:
        raise ValueError('无法识别的密文数据')
    salt = blob[3:19]
    nonce = blob[19:31]
    ct = blob[31:]
    return AESGCM(_derive_key(passphrase, salt)).decrypt(nonce, ct, None)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def unb64(text: str) -> bytes:
    return base64.b64decode(text)