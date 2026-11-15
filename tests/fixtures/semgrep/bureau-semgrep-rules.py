import subprocess
import os
import pickle
import marshal
import json
import yaml
import ssl
import requests
import tempfile

# ruleid: bureau.python.subprocess-command
subprocess.run(["echo", "hello"], shell=True)
# ruleid: bureau.python.subprocess-command
subprocess.run("echo hello")
# ruleid: bureau.python.subprocess-command
subprocess.Popen(f"echo {user_text}")
string_command: str = user_text
# ruleid: bureau.python.subprocess-command
subprocess.run(string_command)
# ok: bureau.python.subprocess-command
subprocess.run(["echo", user_text], shell=False)
# ruleid: bureau.python.os-command
os.system(user_text)
# ruleid: bureau.python.os-command
os.popen(user_text)
# ok: bureau.python.os-command
os.path.join("data", "file")
# ruleid: bureau.python.deserialize
pickle.loads(data)
# ruleid: bureau.python.deserialize
marshal.loads(data)
# ok: bureau.python.deserialize
json.loads(data)
# ruleid: bureau.python.yaml-load
yaml.load(data)
# ok: bureau.python.yaml-load
yaml.load(data, Loader=yaml.SafeLoader)
# ok: bureau.python.yaml-load
yaml.safe_load(data)
# ruleid: bureau.python.unverified-context
ssl._create_unverified_context()
# ok: bureau.python.unverified-context
ssl.create_default_context()
# ruleid: bureau.python.verify-false
requests.get(url, verify=False)
# ok: bureau.python.verify-false
requests.get(url, verify=True)
# ruleid: bureau.python.mktemp
tempfile.mktemp()
# ok: bureau.python.mktemp
tempfile.mkstemp()
