import os

try:
    import requests  # noqa
except Exception:
    os.system("pip install requests")

try:
    from Crypto.Cipher import AES  # noqa  (pycryptodome)
except Exception:
    os.system("pip install pycryptodome")
