import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

req = urllib.request.Request(
    'http://localhost:8000/pre_commit',
    headers={'Content-Type': 'application/json'},
    method='POST',
    data=b'{}'
)

try:
    with urllib.request.urlopen(req, context=ctx) as response:
        print(response.read().decode())
except Exception as e:
    print(f"Error: {e}")
