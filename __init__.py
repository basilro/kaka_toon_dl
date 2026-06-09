import os

try:
    import requests  # noqa
except Exception:
    os.system("pip install requests")

# TLS 지문 둔갑용. 설치 실패(미지원 플랫폼 등)해도 client.py 가 requests 로 폴백.
try:
    from curl_cffi import requests as _cffi  # noqa
except Exception:
    os.system("pip install curl_cffi")

try:
    from Crypto.Cipher import AES  # noqa  (pycryptodome)
except Exception:
    os.system("pip install pycryptodome")

try:
    from PIL import Image  # noqa  (Pillow)
except Exception:
    os.system("pip install Pillow")
