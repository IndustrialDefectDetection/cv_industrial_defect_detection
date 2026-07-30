# Demo burst images

Five `patches` images copied from `data/dataset/images/test/`, chosen because
each one produces at least one detection **above the pipeline's 0.80
confidence gate**. That matters: the gate is what decides whether a detection
ever reaches the agent, and most test images do not clear it.

Measured across one image per class, only 4 of 14 detections cleared 0.80, and
`crazing` produced nothing at all — so a demo built on arbitrary test images
can look completely broken while every service reports healthy.

Used by the end-to-end run:

```bash
# terminal 1 - inference API
cd steel-defect-detection-mlops && .venv/Scripts/activate
uvicorn deployment.api:app --port 8080

# terminal 2 - backend, bridge, dashboard
python Launch.py

# terminal 3 - the camera
cd industrial-data-store-simulation-chatbot
../sample-MES-ClosedLoop-Strands-Agent/.venv/Scripts/python.exe -m bridge.simulator \
    --image-dir ../steel-defect-detection-mlops/data/demo_burst --interval 0.5
```

Expect ~24 stored detections, 6 clearing the gate, and one `AgentAlerts` row
reaching `done` about 70 seconds after the burst.
