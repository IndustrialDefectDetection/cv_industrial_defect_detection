"""
Steel Defect Detection API
FastAPI-based REST API for defect detection inference
"""

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from ultralytics import YOLO
from PIL import Image
import io
import numpy as np
from typing import List, Dict
import time
import os
from pathlib import Path
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Initialize FastAPI app
app = FastAPI(
    title="Steel Defect Detection API",
    description="YOLOv8-based steel surface defect detection service",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus metrics
REQUEST_COUNT = Counter('api_requests_total', 'Total API requests', ['endpoint', 'method'])
INFERENCE_TIME = Histogram('inference_duration_seconds', 'Inference time', ['model'])
DETECTION_COUNT = Counter('detections_total', 'Total defects detected', ['class'])

# Model path - works for both Docker and local
MODEL_PATH = os.getenv(
    "MODEL_PATH", 
    str(Path(__file__).parent.parent / "runs/detect/steel_defect_colab_50_epochs/weights/best.pt")
)
model = None

# Defect class names
CLASS_NAMES = {
    0: "crazing",
    1: "inclusion", 
    2: "patches",
    3: "pitted_surface",
    4: "rolled-in_scale",
    5: "scratches"
}

@app.on_event("startup")
async def load_model():
    """Load YOLOv8 model on startup"""
    global model
    try:
        model = YOLO(MODEL_PATH)
        print(f"[INFO] Model loaded successfully from {MODEL_PATH}")
    except Exception as e:
        print(f"[ERROR] Failed to load model: {e}")
        raise


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Steel Defect Detection API",
        "version": "1.0.0",
        "status": "running",
        "model": "YOLOv8n",
        "endpoints": {
            "health": "/health",
            "predict": "/predict (POST)",
            "batch_predict": "/batch-predict (POST)"
        }
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    REQUEST_COUNT.labels(endpoint='/health', method='GET').inc()
    return {
        "status": "healthy",
        "model_loaded": model is not None,
        "timestamp": time.time()
    }


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/predict")
async def predict_defects(
    file: UploadFile = File(...),
    confidence: float = 0.25
):
    """
    Detect defects in uploaded image
    
    Args:
        file: Image file (JPEG/PNG)
        confidence: Confidence threshold (0.0-1.0)
    
    Returns:
        JSON with detected defects and metadata
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    
    # Validate file type
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    try:
        # Read image
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
        
        # Run inference
        start_time = time.time()
        results = model.predict(
            source=image,
            conf=confidence,
            save=False,
            verbose=False
        )[0]
        inference_time = (time.time() - start_time) * 1000  # ms
        
        # Parse results
        detections = []
        for box in results.boxes:
            class_id = int(box.cls[0])
            conf = float(box.conf[0])
            bbox = box.xyxy[0].tolist()  # [x1, y1, x2, y2]
            
            detections.append({
                "class": CLASS_NAMES.get(class_id, "unknown"),
                "class_id": class_id,
                "confidence": round(conf, 4),
                "bbox": {
                    "x1": round(bbox[0], 2),
                    "y1": round(bbox[1], 2),
                    "x2": round(bbox[2], 2),
                    "y2": round(bbox[3], 2)
                }
            })
        
        return {
            "success": True,
            "image_name": file.filename,
            "image_size": {
                "width": image.width,
                "height": image.height
            },
            "num_detections": len(detections),
            "detections": detections,
            "inference_time_ms": round(inference_time, 2),
            "model": "YOLOv8n",
            "confidence_threshold": confidence
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Inference failed: {str(e)}")


@app.post("/batch-predict")
async def batch_predict_defects(
    files: List[UploadFile] = File(...),
    confidence: float = 0.25
):
    """
    Batch prediction for multiple images
    
    Args:
        files: List of image files
        confidence: Confidence threshold
    
    Returns:
        JSON with results for all images
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    
    results_list = []
    
    for file in files:
        try:
            # Read image
            image_bytes = await file.read()
            image = Image.open(io.BytesIO(image_bytes))
            
            # Run inference
            start_time = time.time()
            results = model.predict(
                source=image,
                conf=confidence,
                save=False,
                verbose=False
            )[0]
            inference_time = (time.time() - start_time) * 1000
            
            # Parse results
            detections = []
            for box in results.boxes:
                class_id = int(box.cls[0])
                conf = float(box.conf[0])
                bbox = box.xyxy[0].tolist()
                
                detections.append({
                    "class": CLASS_NAMES.get(class_id, "unknown"),
                    "confidence": round(conf, 4),
                    "bbox": [round(x, 2) for x in bbox]
                })
            
            results_list.append({
                "filename": file.filename,
                "num_detections": len(detections),
                "detections": detections,
                "inference_time_ms": round(inference_time, 2)
            })
            
        except Exception as e:
            results_list.append({
                "filename": file.filename,
                "error": str(e)
            })
    
    return {
        "success": True,
        "total_images": len(files),
        "results": results_list
    }


@app.get("/model-info")
async def model_info():
    """Get model information"""
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    
    return {
        "model_name": "YOLOv8n",
        "model_path": MODEL_PATH,
        "num_classes": 6,
        "classes": CLASS_NAMES,
        "input_size": "640x640",
        "performance": {
            "mAP50": "76.47%",
            "mAP50-95": "43.28%",
            "precision": "74.79%",
            "recall": "69.39%"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
