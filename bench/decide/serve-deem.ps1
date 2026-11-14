# Serve Deem 9B on 127.0.0.1:8300 for bench/decide. Stop with Ctrl-C in its window.
# DEEM_CHECKPOINT is relative on purpose: the server reads it as an ensemble list
# ("path:weight,..."), so a Windows drive colon would cut an absolute path short.
Set-Location 'D:\Users\Nik\Documents\Projects\forge-bench\decide\models\deem-repo'
$env:DEEM_CHECKPOINT = '..\deem-9b-v1'
$env:DEEM_CALIBRATION = 'serve\calibration\calibration_v13.json'
$env:DEEM_CALIBRATION_KEY = 'v13'
$env:DEEM_MODEL_ID = 'deem-9b'
& 'C:\Users\nschr\Documents\ComfyUI\.venv\Scripts\python.exe' 'serve\deem_server.py' --host 127.0.0.1 --port 8300
