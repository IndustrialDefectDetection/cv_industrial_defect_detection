# Quick Start Guide

Get the Steel Defect Detection system running in 5 minutes.

## Prerequisites

- Python 3.10 or higher installed
- Git installed
- Internet connection

## Step-by-Step Installation

### 1. Clone the Repository

Open your terminal and run:

```bash
git clone https://github.com/ylmz_elff/steel-defect-detection-mlops.git
cd steel-defect-detection-mlops
```

### 2. Set Up Virtual Environment

**Windows:**

```bash
python -m venv venv
venv\Scripts\activate
```

**Mac/Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
```

You should see `(venv)` prefix in your terminal now.

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This will take 2-3 minutes. Grab a coffee ☕

### 4. Verify Model Weights

Check if the trained model exists:

```bash
# Windows
dir "runs\detect\steel_defect_colab_50_epochs\weights\best.pt"

# Mac/Linux
ls runs/detect/steel_defect_colab_50_epochs/weights/best.pt
```

**If the file doesn't exist:**

- Option 1: Download from [Google Drive](https://drive.google.com/your-link) and place it in the above path
- Option 2: Train your own model (see main README.md)

### 5. Run the Application

```bash
streamlit run streamlit_app.py
```

You should see:

```
You can now view your Streamlit app in your browser.

  Local URL: http://localhost:8501
  Network URL: http://192.168.x.x:8501
```

### 6. Test It Out

1. Open http://localhost:8501 in your browser
2. Click "Browse files" or drag-drop a steel surface image
3. See the detection results instantly!

**Don't have test images?**

- Use images from `data/dataset/images/test/` if you have the dataset
- Or upload any steel/metal surface image from the internet

## What's Next?

### Try the API

In a new terminal (keep Streamlit running):

```bash
# Activate venv first
venv\Scripts\activate  # Windows
source venv/bin/activate  # Mac/Linux

# Start API server
uvicorn deployment.api:app --port 8080
```

Then visit http://localhost:8080/docs for interactive API documentation.

### Start MLflow UI

Track training experiments:

```bash
mlflow ui --port 5000
```

Visit http://localhost:5000

### Run Everything with Docker

If you have Docker installed:

```bash
docker-compose -f deployment/docker-compose.full.yml up
```

This starts:

- Streamlit UI at http://localhost:8501
- FastAPI at http://localhost:8080
- MLflow at http://localhost:5000
- Prometheus at http://localhost:9090
- Grafana at http://localhost:3000

## Common Issues

**"command not found: python"**

- Try `python3` instead of `python`

**"No module named 'streamlit'"**

- Make sure you activated the virtual environment (`venv\Scripts\activate`)
- Run `pip install -r requirements.txt` again

**"Model loading failed"**

- Check that model weights file exists (see Step 4)
- Make sure file size is ~6.2MB

**Port already in use**

- Change the port: `streamlit run streamlit_app.py --server.port 8502`

**Still stuck?**

- Check the full README.md for detailed docs
- Open an issue on GitHub

## Success! 🎉

You now have a working steel defect detection system!

Try:

- Uploading different steel images
- Adjusting the confidence threshold
- Batch processing multiple images
- Checking the Analytics tab for training metrics

For advanced usage, training from scratch, and deployment options, see the main [README.md](README.md).
